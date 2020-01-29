from typing import Dict, Optional, Tuple, List

import logging
import re

from email.header import decode_header, make_header
from email.utils import getaddresses
import email.message as message

from django.conf import settings
from django.utils.timezone import timedelta, now as timezone_now

from zerver.lib.actions import internal_send_message, internal_send_private_message, \
    internal_send_stream_message, internal_send_huddle_message, \
    truncate_body, truncate_topic
from zerver.lib.email_mirror_helpers import decode_email_address, \
    get_email_gateway_message_string_from_address, ZulipEmailForwardError
from zerver.lib.email_notifications import convert_html_to_markdown
from zerver.lib.queue import queue_json_publish
from zerver.lib.utils import generate_random_token
from zerver.lib.upload import upload_message_file
from zerver.lib.send_email import FromAddress
from zerver.lib.rate_limiter import RateLimitedObject, rate_limit_entity
from zerver.lib.exceptions import RateLimited
from zerver.models import Stream, Recipient, MissedMessageEmailAddress, \
    get_display_recipient, \
    Message, Realm, UserProfile, get_system_bot, get_user, get_stream_by_id_in_realm

from zproject.backends import is_user_active

logger = logging.getLogger(__name__)

def redact_email_address(error_message: str) -> str:
    if not settings.EMAIL_GATEWAY_EXTRA_PATTERN_HACK:
        domain = settings.EMAIL_GATEWAY_PATTERN.rsplit('@')[-1]
    else:
        # EMAIL_GATEWAY_EXTRA_PATTERN_HACK is of the form '@example.com'
        domain = settings.EMAIL_GATEWAY_EXTRA_PATTERN_HACK[1:]

    address_match = re.search('\\b(\\S*?)@' + domain, error_message)
    if address_match:
        email_address = address_match.group(0)
        # Annotate basic info about the address before scrubbing:
        if is_missed_message_address(email_address):
            redacted_message = error_message.replace(email_address,
                                                     "{} <Missed message address>".format(email_address))
        else:
            try:
                target_stream_id = decode_stream_email_address(email_address)[0].id
                annotated_address = "{} <Address to stream id: {}>".format(email_address, target_stream_id)
                redacted_message = error_message.replace(email_address, annotated_address)
            except ZulipEmailForwardError:
                redacted_message = error_message.replace(email_address,
                                                         "{} <Invalid address>".format(email_address))

        # Scrub the address from the message, to the form XXXXX@example.com:
        string_to_scrub = address_match.groups()[0]
        redacted_message = redacted_message.replace(string_to_scrub, "X" * len(string_to_scrub))
        return redacted_message

    return error_message

def report_to_zulip(error_message: str) -> None:
    if settings.ERROR_BOT is None:
        return
    error_bot = get_system_bot(settings.ERROR_BOT)
    error_stream = Stream.objects.get(name="errors", realm=error_bot.realm)
    send_zulip(settings.ERROR_BOT, error_stream, "email mirror error",
               """~~~\n%s\n~~~""" % (error_message,))

def log_and_report(email_message: message.Message, error_message: str, to: Optional[str]) -> None:
    recipient = to or "No recipient found"
    error_message = "Sender: {}\nTo: {}\n{}".format(email_message.get("From"),
                                                    recipient, error_message)

    error_message = redact_email_address(error_message)
    logger.error(error_message)
    report_to_zulip(error_message)

# Temporary missed message addresses

def generate_missed_message_token() -> str:
    return 'mm' + generate_random_token(32)

def is_missed_message_address(address: str) -> bool:
    try:
        msg_string = get_email_gateway_message_string_from_address(address)
    except ZulipEmailForwardError:
        return False

    return is_mm_32_format(msg_string)

def is_mm_32_format(msg_string: Optional[str]) -> bool:
    '''
    Missed message strings are formatted with a little "mm" prefix
    followed by a randomly generated 32-character string.
    '''
    return msg_string is not None and msg_string.startswith('mm') and len(msg_string) == 34

def get_missed_message_token_from_address(address: str) -> str:
    msg_string = get_email_gateway_message_string_from_address(address)

    if not is_mm_32_format(msg_string):
        raise ZulipEmailForwardError('Could not parse missed message address')

    return msg_string

def get_usable_missed_message_address(address: str) -> MissedMessageEmailAddress:
    token = get_missed_message_token_from_address(address)
    try:
        mm_address = MissedMessageEmailAddress.objects.select_related().get(
            email_token=token,
            timestamp__gt=timezone_now() - timedelta(seconds=MissedMessageEmailAddress.EXPIRY_SECONDS)
        )
    except MissedMessageEmailAddress.DoesNotExist:
        raise ZulipEmailForwardError("Missed message address expired or doesn't exist.")

    if not mm_address.is_usable():
        # Technical, this also checks whether the event is expired,
        # but that case is excluded by the logic above.
        raise ZulipEmailForwardError("Missed message address out of uses.")

    return mm_address

def create_missed_message_address(user_profile: UserProfile, message: Message) -> str:
    if settings.EMAIL_GATEWAY_PATTERN == '':
        logger.warning("EMAIL_GATEWAY_PATTERN is an empty string, using "
                       "NOREPLY_EMAIL_ADDRESS in the 'from' field.")
        return FromAddress.NOREPLY

    mm_address = MissedMessageEmailAddress.objects.create(message=message,
                                                          user_profile=user_profile,
                                                          email_token=generate_missed_message_token())
    return str(mm_address)

def construct_zulip_body(message: message.Message, realm: Realm, show_sender: bool=False,
                         include_quotes: bool=False, include_footer: bool=False,
                         prefer_text: bool=True) -> str:
    body = extract_body(message, include_quotes, prefer_text)
    # Remove null characters, since Zulip will reject
    body = body.replace("\x00", "")
    if not include_footer:
        body = filter_footer(body)

    if not body.endswith('\n'):
        body += '\n'
    body += extract_and_upload_attachments(message, realm)
    body = body.strip()
    if not body:
        body = '(No email body)'

    if show_sender:
        sender = str(make_header(decode_header(message.get("From"))))
        body = "From: %s\n%s" % (sender, body)

    return body

## Sending the Zulip ##

class ZulipEmailForwardUserError(ZulipEmailForwardError):
    pass

def send_zulip(sender: str, stream: Stream, topic: str, content: str) -> None:
    internal_send_message(
        stream.realm,
        sender,
        "stream",
        stream.name,
        truncate_topic(topic),
        truncate_body(content),
        email_gateway=True)

def get_message_part_by_type(message: message.Message, content_type: str) -> Optional[str]:
    charsets = message.get_charsets()

    for idx, part in enumerate(message.walk()):
        if part.get_content_type() == content_type:
            content = part.get_payload(decode=True)
            assert isinstance(content, bytes)
            if charsets[idx]:
                return content.decode(charsets[idx], errors="ignore")
            # If no charset has been specified in the header, assume us-ascii,
            # by RFC6657: https://tools.ietf.org/html/rfc6657
            else:
                return content.decode("us-ascii", errors="ignore")

    return None

def extract_body(message: message.Message, include_quotes: bool=False, prefer_text: bool=True) -> str:
    plaintext_content = extract_plaintext_body(message, include_quotes)
    html_content = extract_html_body(message, include_quotes)

    if plaintext_content is None and html_content is None:
        logging.warning("Content types: %s" % ([part.get_content_type() for part in message.walk()],))
        raise ZulipEmailForwardUserError("Unable to find plaintext or HTML message body")
    if not plaintext_content and not html_content:
        raise ZulipEmailForwardUserError("Email has no nonempty body sections; ignoring.")

    if prefer_text:
        if plaintext_content:
            return plaintext_content
        else:
            assert html_content  # Needed for mypy. Ensured by the validating block above.
            return html_content
    else:
        if html_content:
            return html_content
        else:
            assert plaintext_content  # Needed for mypy. Ensured by the validating block above.
            return plaintext_content

talon_initialized = False
def extract_plaintext_body(message: message.Message, include_quotes: bool=False) -> Optional[str]:
    import talon
    global talon_initialized
    if not talon_initialized:
        talon.init()
        talon_initialized = True

    plaintext_content = get_message_part_by_type(message, "text/plain")
    if plaintext_content is not None:
        if include_quotes:
            return plaintext_content
        else:
            return talon.quotations.extract_from_plain(plaintext_content)
    else:
        return None

def extract_html_body(message: message.Message, include_quotes: bool=False) -> Optional[str]:
    import talon
    global talon_initialized
    if not talon_initialized:  # nocoverage
        talon.init()
        talon_initialized = True

    html_content = get_message_part_by_type(message, "text/html")
    if html_content is not None:
        if include_quotes:
            return convert_html_to_markdown(html_content)
        else:
            return convert_html_to_markdown(talon.quotations.extract_from_html(html_content))
    else:
        return None

def filter_footer(text: str) -> str:
    # Try to filter out obvious footers.
    possible_footers = [line for line in text.split("\n") if line.strip() == "--"]
    if len(possible_footers) != 1:
        # Be conservative and don't try to scrub content if there
        # isn't a trivial footer structure.
        return text

    return text.partition("--")[0].strip()

def extract_and_upload_attachments(message: message.Message, realm: Realm) -> str:
    user_profile = get_system_bot(settings.EMAIL_GATEWAY_BOT)

    attachment_links = []
    for part in message.walk():
        content_type = part.get_content_type()
        filename = part.get_filename()
        if filename:
            attachment = part.get_payload(decode=True)
            if isinstance(attachment, bytes):
                s3_url = upload_message_file(filename, len(attachment), content_type,
                                             attachment,
                                             user_profile,
                                             target_realm=realm)
                formatted_link = "[%s](%s)" % (filename, s3_url)
                attachment_links.append(formatted_link)
            else:
                logger.warning("Payload is not bytes (invalid attachment %s in message from %s)." %
                               (filename, message.get("From")))

    return '\n'.join(attachment_links)

def decode_stream_email_address(email: str) -> Tuple[Stream, Dict[str, bool]]:
    token, options = decode_email_address(email)

    try:
        stream = Stream.objects.get(email_token=token)
    except Stream.DoesNotExist:
        raise ZulipEmailForwardError("Bad stream token from email recipient " + email)

    return stream, options

def find_emailgateway_recipient(message: message.Message) -> str:
    # We can't use Delivered-To; if there is a X-Gm-Original-To
    # it is more accurate, so try to find the most-accurate
    # recipient list in descending priority order
    recipient_headers = ["X-Gm-Original-To", "Delivered-To",
                         "Resent-To", "Resent-CC", "To", "CC"]

    pattern_parts = [re.escape(part) for part in settings.EMAIL_GATEWAY_PATTERN.split('%s')]
    match_email_re = re.compile(".*?".join(pattern_parts))

    header_addresses = [str(addr)
                        for recipient_header in recipient_headers
                        for addr in message.get_all(recipient_header, [])]

    for addr_tuple in getaddresses(header_addresses):
        if match_email_re.match(addr_tuple[1]):
            return addr_tuple[1]

    raise ZulipEmailForwardError("Missing recipient in mirror email")

def strip_from_subject(subject: str) -> str:
    # strips RE and FWD from the subject
    # from: https://stackoverflow.com/questions/9153629/regex-code-for-removing-fwd-re-etc-from-email-subject
    reg = r"([\[\(] *)?\b(RE|FWD?) *([-:;)\]][ :;\])-]*|$)|\]+ *$"
    stripped = re.sub(reg, "", subject, flags = re.IGNORECASE | re.MULTILINE)
    return stripped.strip()

def is_forwarded(subject: str) -> bool:
    # regex taken from strip_from_subject, we use it to detect various forms
    # of FWD at the beginning of the subject.
    reg = r"([\[\(] *)?\b(FWD?) *([-:;)\]][ :;\])-]*|$)|\]+ *$"
    return bool(re.match(reg, subject, flags=re.IGNORECASE))

def process_stream_message(to: str, message: message.Message) -> None:
    subject_header = str(make_header(decode_header(message.get("Subject", ""))))
    subject = strip_from_subject(subject_header) or "(no topic)"

    stream, options = decode_stream_email_address(to)
    # Don't remove quotations if message is forwarded, unless otherwise specified:
    if 'include_quotes' not in options:
        options['include_quotes'] = is_forwarded(subject_header)

    body = construct_zulip_body(message, stream.realm, **options)
    send_zulip(settings.EMAIL_GATEWAY_BOT, stream, subject, body)
    logger.info("Successfully processed email to %s (%s)" % (
        stream.name, stream.realm.string_id))

def process_missed_message(to: str, message: message.Message) -> None:
    mm_address = get_usable_missed_message_address(to)
    mm_address.increment_times_used()

    user_profile = mm_address.user_profile
    topic = mm_address.message.topic_name()

    if mm_address.message.recipient.type == Recipient.PERSONAL:
        # We need to reply to the sender so look up their personal recipient_id
        recipient = mm_address.message.sender.recipient
    else:
        recipient = mm_address.message.recipient

    if not is_user_active(user_profile):
        logger.warning("Sending user is not active. Ignoring this missed message email.")
        return

    body = construct_zulip_body(message, user_profile.realm)

    if recipient.type == Recipient.STREAM:
        stream = get_stream_by_id_in_realm(recipient.type_id, user_profile.realm)
        internal_send_stream_message(
            user_profile.realm, user_profile, stream,
            topic, body
        )
        recipient_str = stream.name
    elif recipient.type == Recipient.PERSONAL:
        display_recipient = get_display_recipient(recipient)
        assert not isinstance(display_recipient, str)
        recipient_str = display_recipient[0]['email']
        recipient_user = get_user(recipient_str, user_profile.realm)
        internal_send_private_message(user_profile.realm, user_profile,
                                      recipient_user, body)
    elif recipient.type == Recipient.HUDDLE:
        display_recipient = get_display_recipient(recipient)
        assert not isinstance(display_recipient, str)
        emails = [user_dict['email'] for user_dict in display_recipient]
        recipient_str = ', '.join(emails)
        internal_send_huddle_message(user_profile.realm, user_profile,
                                     emails, body)
    else:
        raise AssertionError("Invalid recipient type!")

    logger.info("Successfully processed email from user %s to %s" % (
        user_profile.id, recipient_str))

def process_message(message: message.Message, rcpt_to: Optional[str]=None) -> None:
    to = None  # type: Optional[str]

    try:
        if rcpt_to is not None:
            to = rcpt_to
        else:
            to = find_emailgateway_recipient(message)

        if is_missed_message_address(to):
            process_missed_message(to, message)
        else:
            process_stream_message(to, message)
    except ZulipEmailForwardError as e:
        if isinstance(e, ZulipEmailForwardUserError):
            # TODO: notify sender of error, retry if appropriate.
            logging.warning(str(e))
        else:
            log_and_report(message, str(e), to)

def validate_to_address(rcpt_to: str) -> None:
    if is_missed_message_address(rcpt_to):
        get_usable_missed_message_address(rcpt_to)
    else:
        decode_stream_email_address(rcpt_to)

def mirror_email_message(data: Dict[str, str]) -> Dict[str, str]:
    rcpt_to = data['recipient']
    try:
        validate_to_address(rcpt_to)
    except ZulipEmailForwardError as e:
        return {
            "status": "error",
            "msg": "5.1.1 Bad destination mailbox address: {}".format(e)
        }

    queue_json_publish(
        "email_mirror",
        {
            "message": data['msg_text'],
            "rcpt_to": rcpt_to
        }
    )
    return {"status": "success"}

# Email mirror rate limiter code:

class RateLimitedRealmMirror(RateLimitedObject):
    def __init__(self, realm: Realm) -> None:
        self.realm = realm

    def key_fragment(self) -> str:
        return "emailmirror:{}:{}".format(type(self.realm), self.realm.id)

    def rules(self) -> List[Tuple[int, int]]:
        return settings.RATE_LIMITING_MIRROR_REALM_RULES

    def __str__(self) -> str:
        return self.realm.string_id

def rate_limit_mirror_by_realm(recipient_realm: Realm) -> None:
    entity = RateLimitedRealmMirror(recipient_realm)
    ratelimited = rate_limit_entity(entity)[0]

    if ratelimited:
        raise RateLimited()
