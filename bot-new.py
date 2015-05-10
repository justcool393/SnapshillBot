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

USER_AGENT = "Archives to archive.is (/u/justcool393) v1.1"
REDDIT_DOMAIN = "api.reddit.com"
INFO = "/r/SnapshillBot"
CONTACT = "/message/compose?to=\/r\/SnapshillBot"
ARCHIVE_SELF = os.environ.get('ARCHIVE_SELF') is "1"
SUBMISSION_SCAN_COUNT = 10
WAIT_TIME = 4 * 60

archived = []
user = os.environ['REDDIT_USER']


def main():
    r = praw.Reddit(USER_AGENT, domain=REDDIT_DOMAIN)
    r.login(user, os.environ.get("REDDIT_PASS"))
    logging.info("Logged in and started post archiving.")
    add_archived(r)

    check_at = 3600
    last_checked = 0
    times_zero = 1

    arch = archive_submissions(r, 50, 0)
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

        arch += archive_submissions(r, SUBMISSION_SCAN_COUNT, WAIT_TIME)


def add_archived(r):
    for c in r.user.get_comments(sort='new', limit=None):
        pid = c.parent_id
        if pid is None or pid in archived:
            continue
        archived.append(pid)


def archive_submissions(r, count, delay):
    archived_posts = 0

    for submission in r.get_new(limit=count):
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


def get_archive_link(data):
    return re.findall("http[s]?://archive.is/[0-z]{1,6}", data)[0]


def archive_and_post(s):
    if s.is_self and not ARCHIVE_SELF:
        return False
    arch_post = archive(s.url)
    return post(s, arch_post)


def archive_self(s):
    


def archive(url):
    pairs = {"url": url}
    res = urllib2.urlopen("https://archive.is/submit/", urllib.urlencode(pairs))
    return get_archive_link(res.read())


def post(s, archive_link):
    comment = """
Automatically archived [here]({link}). {quip}

*^(I am a bot.) ^\([*Info*]({info}) ^/ ^[*Contact*]({contact}))*
"""

    try:
        s.add_comment(
            comment.format(link=archive_link, info=INFO, contact=CONTACT,
                           quip=get_quip(s.subreddit)))
    except Exception as ex:
        logging.error("Error adding comment (Submission ID: " + str(s.id) + ")")
        logging.error(str(ex))
        return False
    return True

def get_quip(subreddit):
    subreddit = subreddit.display_name
    if subreddit == "SubredditDrama":
        return "Just helping out ttumblrbots until they get back. Here is [" \
               "some dogs](http://www.omfgdogs.com) while we wait..."
    return ""


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