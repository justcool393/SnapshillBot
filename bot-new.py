import logging
import os
import praw
import re
import random
import time
import traceback
import urlparse
import urllib2
import urllib
import sys


INFO = "/r/SSBot"
CONTACT = "/message/compose?to=\/r\/SnapshillBot"
ARCHIVE_SELF = os.environ['ARCHIVE_SELF'] is "1"
SUBMISSION_SCAN_COUNT = 10
SUBREDDIT = "Buttcoin+Oppression+RedditCensorship+SSBot+TheBluePill+undelete"

archived = []
user = os.environ['REDDIT_USER']


def main():
    r = praw.Reddit("Snapshot Bot (/u/justcool393)", domain="api.reddit.com")
    r.login(user, os.environ['REDDIT_PASS'])
    logging.info("Logged in and started post archiving.")
    add_archived(r)
    s = r.get_subreddit(SUBREDDIT)

    check_at = 3600
    last_checked = 0
    times_zero = 1

    arch = archive_submissions(r, s, 50, 90)
    # Check the last 50 posts on startup
    while True:
        if time.time() - last_checked > check_at:
            last_checked = time.time()
            if arch == 0:
                times_zero += 1
            else:
                logging.info("Last " + str((check_at * times_zero) / 60)
                             + "min: " + str(arch))

                arch = 0
                times_zero = 1

        arch += archive_submissions(r, s, SUBMISSION_SCAN_COUNT, 240)


def add_archived(r):
    for c in r.user.get_comments(sort='new', limit=None):
        pid = c.parent_id
        if pid is None or pid in archived:
            continue
        archived.append(pid)


def archive_submissions(r, s, count, delay):
    archived_posts = 0

    for submission in s.get_new(limit=count):
        if submission.id in archived:
            continue

        submission.replace_more_comments(limit=None, threshold=0)

        commented = check_commented(submission)

        if commented:
            archived.append(submission.id)
            continue

        try:
            if archive_and_post(submission):
                archived_posts += 1
                archived.append(submission.id)
        except UnicodeEncodeError:
            logging.error("Error (UEE): Submission ID: " + submission.id + ")")

    time.sleep(delay)
    return archived_posts


def check_commented(s):
    flat_comments = praw.helpers.flatten_tree(s.comments)
    for c in flat_comments:
        if c.author is None:
            continue
        if c.author.name == user:
            return True
    return False


def get_response(url, data):
    res = urllib2.urlopen(fix_url(url), data)
    return res.read()


def get_redirected_url(data):
    return re.findall('http[s]?://archive.today/[0-z]{1,6}', data)[0]


def archive_and_post(s):
    if s.is_self and not ARCHIVE_SELF:
        return False
    arch_post = archive(s.url)
    return post(s, arch_post)


def archive(url):
    pairs = [{"url", url}]
    return get_redirected_url(get_response("https://archive.today/submit/",
                                           urllib.urlencode(pairs)))


def post(s, archive_link):
    comment = """
Automatically archived [here]({link}).

*I am a bot. ([Info]({info}) | [Contact]({contact}))*
"""

    try:
        s.add_comment(
            comment.format(link=archive_link, info=INFO, contact=CONTACT))
    except Exception as ex:
        logging.error(
            "Error adding comment. (Submission ID: " + str(s.id) + ")")
        logging.error(str(ex))
        return False
    return True


def urlEncodeNonAscii(b):
    return re.sub('[\x80-\xFF]', lambda c: '%%%02x' % ord(c.group(0)), b)


def fix_url(iri):
    parts = urlparse.urlparse(iri)
    return urlparse.urlunparse(
        part.encode('idna') if parti == 1 else urlEncodeNonAscii(
            part.encode('utf-8'))
        for parti, part in enumerate(parts)
    )


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("requests").setLevel(logging.WARNING)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)

    root.addHandler(ch)


def log_crash(e):
    logging.error("Error occurred in the bot restarting in 15 seconds...")
    logging.error("Details: " + str(e))
    traceback.print_exc()
    time.sleep(15)
    sys.exit(1)  # Signal to the host that we crashed


try:
    setup_logging()
    main()
except (NameError, SyntaxError, AttributeError) as e:
    logging.error(str(e))
    time.sleep(86400)  # Sleep for 1 day so we don't restart.
except Exception as e:
    log_crash(e)