from typing import Any, Dict, List, Optional, Text, Union, Tuple

import logging
import re

from email.header import decode_header, Header
import email.message as message

from django.conf import settings
from six import unichr

import zerver.lib.actions
from zerver.lib.notifications import convert_html_to_markdown
from zerver.lib.queue import queue_json_publish
from zerver.lib.redis_utils import get_redis_client
from zerver.lib.upload import upload_message_image
from zerver.lib.utils import generate_random_token
from zerver.lib.str_utils import force_text
from zerver.lib.send_email import FromAddress
from zerver.models import Stream, Recipient, \
    get_user_profile_by_id, get_display_recipient, get_personal_recipient, \
    Message, Realm, UserProfile, get_system_bot, get_user
import talon
from talon import quotations

talon.init()

logger = logging.getLogger(__name__)

def redact_stream(error_message: Text) -> Text:
    domain = settings.EMAIL_GATEWAY_PATTERN.rsplit('@')[-1]
    stream_match = re.search('\\b(.*?)@' + domain, error_message)
    if stream_match:
        stream_name = stream_match.groups()[0]
        return error_message.replace(stream_name, "X" * len(stream_name))
    return error_message

def report_to_zulip(error_message: Text) -> None:
    if settings.ERROR_BOT is None:
        return
    error_bot = get_system_bot(settings.ERROR_BOT)
    error_stream = Stream.objects.get(name="errors", realm=error_bot.realm)
    send_zulip(settings.ERROR_BOT, error_stream, "email mirror error",
               """~~~\n%s\n~~~""" % (error_message,))

def log_and_report(email_message: message.Message, error_message: Text, debug_info: Dict[str, Any]) -> None:
    scrubbed_error = u"Sender: %s\n%s" % (email_message.get("From"),
                                          redact_stream(error_message))

    if "to" in debug_info:
        scrubbed_error = "Stream: %s\n%s" % (redact_stream(debug_info["to"]),
                                             scrubbed_error)

    if "stream" in debug_info:
        scrubbed_error = "Realm: %s\n%s" % (debug_info["stream"].realm.string_id,
                                            scrubbed_error)

    logger.error(scrubbed_error)
    report_to_zulip(scrubbed_error)


# Temporary missed message addresses

redis_client = get_redis_client()


def missed_message_redis_key(token: Text) -> Text:
    return 'missed_message:' + token


def is_missed_message_address(address: Text) -> bool:
    try:
        msg_string = get_email_gateway_message_string_from_address(address)
        return is_mm_32_format(msg_string)
    except ZulipEmailUnrecognizedAddressError:
        return None

def is_mm_32_format(msg_string: Optional[Text]) -> bool:
    '''
    Missed message strings are formatted with a little "mm" prefix
    followed by a randomly generated 32-character string.
    '''
    return msg_string is not None and msg_string.startswith('mm') and len(msg_string) == 34

def get_missed_message_token_from_address(address: Text) -> Text:
    try:
        msg_string = get_email_gateway_message_string_from_address(address)

        if not is_mm_32_format(msg_string):
            raise ZulipEmailForwardError('Could not parse missed message address')

        # strip off the 'mm' before returning the redis key
        return msg_string[2:]
    except ZulipEmailUnrecognizedAddressError:
        raise ZulipEmailForwardError('Address not recognized by gateway.')

def create_missed_message_address(user_profile: UserProfile, message: Message) -> str:
    if settings.EMAIL_GATEWAY_PATTERN == '':
        logger.warning("EMAIL_GATEWAY_PATTERN is an empty string, using "
                       "NOREPLY_EMAIL_ADDRESS in the 'from' field.")
        return FromAddress.NOREPLY

    if message.recipient.type == Recipient.PERSONAL:
        # We need to reply to the sender so look up their personal recipient_id
        recipient_id = get_personal_recipient(message.sender_id).id
    else:
        recipient_id = message.recipient_id

    data = {
        'user_profile_id': user_profile.id,
        'recipient_id': recipient_id,
        'subject': message.subject.encode('utf-8'),
    }

    while True:
        token = generate_random_token(32)
        key = missed_message_redis_key(token)
        if redis_client.hsetnx(key, 'uses_left', 1):
            break

    with redis_client.pipeline() as pipeline:
        pipeline.hmset(key, data)
        pipeline.expire(key, 60 * 60 * 24 * 5)
        pipeline.execute()

    address = 'mm' + token
    return settings.EMAIL_GATEWAY_PATTERN % (address,)


def mark_missed_message_address_as_used(address: Text) -> None:
    token = get_missed_message_token_from_address(address)
    key = missed_message_redis_key(token)
    with redis_client.pipeline() as pipeline:
        pipeline.hincrby(key, 'uses_left', -1)
        pipeline.expire(key, 60 * 60 * 24 * 5)
        new_value = pipeline.execute()[0]
    if new_value < 0:
        redis_client.delete(key)
        raise ZulipEmailForwardError('Missed message address has already been used')

def construct_zulip_body(message: message.Message, realm: Realm) -> Text:
    body = extract_body(message)
    # Remove null characters, since Zulip will reject
    body = body.replace("\x00", "")
    body = filter_footer(body)
    body += extract_and_upload_attachments(message, realm)
    body = body.strip()
    if not body:
        body = '(No email body)'
    return body

def send_to_missed_message_address(address: Text, message: message.Message) -> None:
    token = get_missed_message_token_from_address(address)
    key = missed_message_redis_key(token)
    result = redis_client.hmget(key, 'user_profile_id', 'recipient_id', 'subject')
    if not all(val is not None for val in result):
        raise ZulipEmailForwardError('Missing missed message address data')
    user_profile_id, recipient_id, subject_b = result  # type: (bytes, bytes, bytes)

    user_profile = get_user_profile_by_id(user_profile_id)
    recipient = Recipient.objects.get(id=recipient_id)
    display_recipient = get_display_recipient(recipient)

    body = construct_zulip_body(message, user_profile.realm)

    if recipient.type == Recipient.STREAM:
        assert isinstance(display_recipient, str)
        recipient_str = display_recipient
        zerver.lib.actions.internal_send_stream_message(user_profile.realm, user_profile, recipient_str,
                                              subject_b.decode('utf-8'), body)
    elif recipient.type == Recipient.PERSONAL:
        assert not isinstance(display_recipient, str)
        recipient_str = display_recipient[0]['email']
        recipient_user = get_user(recipient_str, user_profile.realm)
        zerver.lib.actions.internal_send_private_message(user_profile.realm, user_profile,
                                               recipient_user, body)
    elif recipient.type == Recipient.HUDDLE:
        assert not isinstance(display_recipient, str)
        emails = [user_dict['email'] for user_dict in display_recipient]
        recipient_str = ', '.join(emails)
        zerver.lib.actions.internal_send_huddle_message(user_profile.realm, user_profile,
                                              emails, body)
    else:
        raise AssertionError("Invalid recipient type!")

    logger.info("Successfully processed email from %s to %s" % (
        user_profile.email, recipient_str))

## Sending the Zulip ##

class ZulipEmailForwardError(Exception):
    pass

def send_zulip(sender: Text, stream: Stream, topic: Text, content: Text) -> None:
    zerver.lib.actions.internal_send_message(
        stream.realm,
        sender,
        "stream",
        stream.name,
        topic[:60],
        content[:2000],
        email_gateway=True)

def valid_stream(stream_name: Text, token: Text) -> bool:
    try:
        stream = Stream.objects.get(email_token=token)
        return stream.name.lower() == stream_name.lower()
    except Stream.DoesNotExist:
        return False

def get_message_part_by_type(message: message.Message, content_type: Text) -> Optional[Text]:
    charsets = message.get_charsets()

    for idx, part in enumerate(message.walk()):
        if part.get_content_type() == content_type:
            content = part.get_payload(decode=True)
            assert isinstance(content, bytes)
            if charsets[idx]:
                return content.decode(charsets[idx], errors="ignore")
    return None

def extract_body(message: message.Message) -> Text:
    # If the message contains a plaintext version of the body, use
    # that.
    plaintext_content = get_message_part_by_type(message, "text/plain")
    if plaintext_content:
        return quotations.extract_from_plain(plaintext_content)

    # If we only have an HTML version, try to make that look nice.
    html_content = get_message_part_by_type(message, "text/html")
    if html_content:
        return convert_html_to_markdown(quotations.extract_from_html(html_content))

    raise ZulipEmailForwardError("Unable to find plaintext or HTML message body")

def filter_footer(text: Text) -> Text:
    # Try to filter out obvious footers.
    possible_footers = [line for line in text.split("\n") if line.strip().startswith("--")]
    if len(possible_footers) != 1:
        # Be conservative and don't try to scrub content if there
        # isn't a trivial footer structure.
        return text

    return text.partition("--")[0].strip()

def extract_and_upload_attachments(message: message.Message, realm: Realm) -> Text:
    user_profile = get_system_bot(settings.EMAIL_GATEWAY_BOT)
    attachment_links = []

    payload = message.get_payload()
    if not isinstance(payload, list):
        # This is not a multipart message, so it can't contain attachments.
        return ""

    for part in payload:
        content_type = part.get_content_type()
        filename = part.get_filename()
        if filename:
            attachment = part.get_payload(decode=True)
            if isinstance(attachment, bytes):
                s3_url = upload_message_image(filename, len(attachment), content_type,
                                              attachment,
                                              user_profile,
                                              target_realm=realm)
                formatted_link = "[%s](%s)" % (filename, s3_url)
                attachment_links.append(formatted_link)
            else:
                logger.warning("Payload is not bytes (invalid attachment %s in message from %s)." %
                               (filename, message.get("From")))

    return "\n".join(attachment_links)

def extract_and_validate(email: Text) -> Stream:
    temp = decode_email_address(email)
    if temp is None:
        raise ZulipEmailForwardError("Malformed email recipient " + email)
    stream_name, token = temp

    if not valid_stream(stream_name, token):
        raise ZulipEmailForwardError("Bad stream token from email recipient " + email)

    return Stream.objects.get(email_token=token)

def find_emailgateway_recipient(message: message.Message) -> Text:
    # We can't use Delivered-To; if there is a X-Gm-Original-To
    # it is more accurate, so try to find the most-accurate
    # recipient list in descending priority order
    recipient_headers = ["X-Gm-Original-To", "Delivered-To", "To"]
    recipients = []  # type: List[Union[Text, Header]]
    for recipient_header in recipient_headers:
        r = message.get_all(recipient_header, None)
        if r:
            recipients = r
            break

    pattern_parts = [re.escape(part) for part in settings.EMAIL_GATEWAY_PATTERN.split('%s')]
    match_email_re = re.compile(".*?".join(pattern_parts))
    for recipient_email in [str(recipient) for recipient in recipients]:
        if match_email_re.match(recipient_email):
            return recipient_email

    raise ZulipEmailForwardError("Missing recipient in mirror email")

def process_stream_message(to: Text, subject: Text, message: message.Message,
                           debug_info: Dict[str, Any]) -> None:
    stream = extract_and_validate(to)
    body = construct_zulip_body(message, stream.realm)
    debug_info["stream"] = stream
    send_zulip(settings.EMAIL_GATEWAY_BOT, stream, subject, body)
    logger.info("Successfully processed email to %s (%s)" % (
        stream.name, stream.realm.string_id))

def process_missed_message(to: Text, message: message.Message, pre_checked: bool) -> None:
    if not pre_checked:
        mark_missed_message_address_as_used(to)
    send_to_missed_message_address(to, message)

def process_message(message: message.Message, rcpt_to: Optional[Text]=None, pre_checked: bool=False) -> None:
    subject_header = message.get("Subject", "(no subject)")
    encoded_subject, encoding = decode_header(subject_header)[0]
    if encoding is None:
        subject = force_text(encoded_subject)  # encoded_subject has type str when encoding is None
    else:
        try:
            subject = encoded_subject.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            subject = "(unreadable subject)"

    debug_info = {}

    try:
        if rcpt_to is not None:
            to = rcpt_to
        else:
            to = find_emailgateway_recipient(message)
        debug_info["to"] = to

        if is_missed_message_address(to):
            process_missed_message(to, message, pre_checked)
        else:
            process_stream_message(to, subject, message, debug_info)
    except ZulipEmailForwardError as e:
        # TODO: notify sender of error, retry if appropriate.
        log_and_report(message, str(e), debug_info)


def mirror_email_message(data: Dict[Text, Text]) -> Dict[str, str]:
    rcpt_to = data['recipient']
    if is_missed_message_address(rcpt_to):
        try:
            mark_missed_message_address_as_used(rcpt_to)
        except ZulipEmailForwardError:
            return {
                "status": "error",
                "msg": "5.1.1 Bad destination mailbox address: "
                       "Bad or expired missed message address."
            }
    else:
        try:
            extract_and_validate(rcpt_to)
        except ZulipEmailForwardError:
            return {
                "status": "error",
                "msg": "5.1.1 Bad destination mailbox address: "
                       "Please use the address specified in your Streams page."
            }
    queue_json_publish(
        "email_mirror",
        {
            "message": data['msg_text'],
            "rcpt_to": rcpt_to
        }
    )
    return {"status": "success"}

def encode_email_address(stream: Stream) -> Text:
    return encode_email_address_helper(stream.name, stream.email_token)

def encode_email_address_helper(name: Text, email_token: Text) -> Text:
    # Some deployments may not use the email gateway
    if settings.EMAIL_GATEWAY_PATTERN == '':
        return ''

    # Given the fact that we have almost no restrictions on stream names and
    # that what characters are allowed in e-mail addresses is complicated and
    # dependent on context in the address, we opt for a very simple scheme:
    #
    # Only encode the stream name (leave the + and token alone). Encode
    # everything that isn't alphanumeric plus _ as the percent-prefixed integer
    # ordinal of that character, padded with zeroes to the maximum number of
    # bytes of a UTF-8 encoded Unicode character.
    encoded_name = re.sub("\W", lambda x: "%" + str(ord(x.group(0))).zfill(4), name)
    encoded_token = "%s+%s" % (encoded_name, email_token)
    return settings.EMAIL_GATEWAY_PATTERN % (encoded_token,)

def get_email_gateway_message_string_from_address(address: Text) -> Optional[Text]:
    pattern_parts = [re.escape(part) for part in settings.EMAIL_GATEWAY_PATTERN.split('%s')]
    if settings.EMAIL_GATEWAY_EXTRA_PATTERN_HACK:
        # Accept mails delivered to any Zulip server
        pattern_parts[-1] = settings.EMAIL_GATEWAY_EXTRA_PATTERN_HACK
    match_email_re = re.compile("(.*?)".join(pattern_parts))
    match = match_email_re.match(address)

    if not match:
        raise ZulipEmailUnrecognizedAddressError("No matching address found")

    msg_string = match.group(1)

    return msg_string

def decode_email_address(email: Text) -> Optional[Tuple[Text, Text]]:
    # Perform the reverse of encode_email_address. Returns a tuple of (streamname, email_token)

    msg_string = get_email_gateway_message_string_from_address(email)

    if '.' in msg_string:
        # Workaround for Google Groups and other programs that don't accept emails
        # that have + signs in them (see Trac #2102)
        encoded_stream_name, token = msg_string.split('.')
    else:
        encoded_stream_name, token = msg_string.split('+')
    stream_name = re.sub("%\d{4}", lambda x: unichr(int(x.group(0)[1:])), encoded_stream_name)
    return stream_name, token

class ZulipEmailUnrecognizedAddressError(Exception):
    pass
