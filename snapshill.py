import gevent
from gevent import monkey
from gevent.pool import Pool
monkey.patch_all(thread=False, select=False)

import timeit

import logging
import os
import praw
import re
import random
import sqlite3
import time
import traceback
import warnings

import requests

from bs4 import BeautifulSoup
from html.parser import unescape
from urllib.parse import urlencode, urlparse, urljoin

from praw.exceptions import APIException, ClientException
from prawcore.exceptions import ResponseException

USER_AGENT = "Archives to archive.is and archive.org (/r/SnapshillBot) v2.0"
INFO = "/r/SnapshillBot"
CONTACT = "/message/compose?to=\/r\/SnapshillBot"

ARCHIVE_ORG_FORMAT = "%Y%m%d%H%M%S"
MEGALODON_JP_FORMAT = "%Y-%m%d-%H%M-%S"

DB_FILE = os.environ.get("DATABASE", "snapshill.sqlite3")

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
USERNAME = os.environ.get("REDDIT_USER")
PASSWORD = os.environ.get("REDDIT_PASS")

REDDIT_LOCATION = "http://www.reddit.com/"
EXTENDED_POST_SUBREDDIT = "SnapshillBotEx"

REDDIT_PATTERN = re.compile("https?://(([A-z]{2})(-[A-z]{2})"
                            "?|beta|i|m|pay|ssl|www)\.?reddit\.com/?")
SUBREDDIT_OR_USER = re.compile("/?(u|user|r)/[^\/]+/?$")

MAX_COMMENT_LENGTH = 10000
MAX_TITLE_LENGTH = 35

RECOVERABLE_EXC = (APIException,
                   ClientException,
                   ResponseException)

ERROR_MESSAGE = "could not auto-archive; click to resubmit!"

REDDIT_WAIT_TIME = 2  # Sites can perform one reddit lookup per two seconds.

loglevel = logging.DEBUG if os.environ.get("DEBUG") == "true" else logging.INFO
logging.basicConfig(level=loglevel,
                    format="[%(asctime)s] [%(levelname)s] %(message)s")
log = logging.getLogger("snapshill")
logging.getLogger("requests").setLevel(loglevel)
warnings.simplefilter("ignore")  # Ignore ResourceWarnings (because screw them)

r = praw.Reddit(client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                user_agent=USER_AGENT,
                username=USERNAME,
                password=PASSWORD)
s = requests.Session()

#####################
# Utility functions #
#####################
def get_footer():
    return "*^(I am a bot.) ^\([*Info*]({info}) ^/ ^[*Contact*]({" \
           "contact}))*".format(info=INFO, contact=CONTACT)

def should_notify(name):
    """
    Looks for other snapshot bot comments in the comment chain and doesn't
    post if they do.
    :param submission: Submission to check
    :return: If we should comment or not
    """
    cur.execute("SELECT * FROM links WHERE id=?", (name,))

    if cur.fetchone():
        return False

    return True

def store_notification(post_id, reply_id):
    cur.execute("INSERT INTO links (id, reply) VALUES (?, ?)",
                (post_id, reply_id))
    db.commit()

def log_error(e):
    log.error("Unexpected {}:\n{}".format(e.__class__.__name__,
                                          traceback.format_exc()))


################
# IO Functions #
################
def handle_post(post, snapshillbot, reddit_pool = None, notify_pool = None):
    jobs = []

    if reddit_pool is None:
        reddit_pool = Pool(1)

    if notify_pool is None:
        notify_pool = Pool(1)

    for link in post.links:
        if link.is_reddit():
            jobs.append(reddit_pool.spawn(create_reddit_archives, link))
        else:
            for archive in link.archives:
                jobs.append(create_archive(archive))

    gevent.joinall(jobs)
    gevent.wait(notify_pool.spawn(notify, post, snapshillbot))

def create_archive(archive):
    return gevent.spawn(archive.archive)

def create_reddit_archives(link):
    archives = []

    for archive in link.archives:
        archives.append(gevent.spawn(archive.archive))

    gevent.joinall(archives)

    # We ratelimit other sites when they visit reddit.
    gevent.sleep(REDDIT_WAIT_TIME)

def notify(post, snapshillbot):
    comment = Notification(post, snapshillbot.get_header(post.submission.subreddit)).notify()

    if comment:
        store_notification(post.name, comment.name)


###########
# Classes #
###########
class Archive:
    site_name = None

    def __init__(self, url):
        self.url = url
        self.archived = None

    def archive(self):
        log.debug("Archiving {} with {}".format(self.url, self.site_name))
        self.archived = self._archive()

        return self.archived

    def name(self):
        if self.archived:
            return self.site_name
        else:
            return "_{}\*_".format(self.site_name)

    def link(self):
        return self.archived or self.error_message()

    def resubmit_link(self):
        return None

    def error_message(self):
        return "{} \"{}\"".format(self.resubmit_link(), ERROR_MESSAGE)

    def build(self):
        return "[{}]({})".format(self.name(), self.link())


class ArchiveIsArchive(Archive):
    site_name = "archive.is"

    def _archive(self):
        """
        Archives to archive.is. Returns a 200, and we have to find the
        JavaScript redirect through a regex in the response text.
        :return: URL of the archive or False if an error occurred
        """
        pairs = {"url": self.url}

        try:
            res = s.post("https://archive.is/submit/", pairs, verify=False)
        except RECOVERABLE_EXC:
            return False

        found = re.findall("http[s]?://archive.is/[0-z]{1,6}", res.text)

        if len(found) < 1:
            return False

        return found[0]

    def resubmit_link(self):
        pairs = {"url": self.url, "run": 1}
        return "https://archive.is/?" + urlencode(pairs)


class ArchiveOrgArchive(Archive):
    site_name = "archive.org"

    def _archive(self):
        """
        Archives to archive.org. The website gives a 403 Forbidden when the
        archive cannot be generated (because it follows robots.txt rules)
        :return: URL of the archive, False if an error occurred, or None if
        we cannot archive this page.
        """
        try:
            s.get("https://web.archive.org/save/" + self.url)
        except RECOVERABLE_EXC as e:
            return False

        date = time.strftime(ARCHIVE_ORG_FORMAT, time.gmtime())

        return "https://web.archive.org/" + date + "/" + self.url

    def resubmit_link(self):
        return "https://web.archive.org/save/" + self.url


class MegalodonJPArchive(Archive):
    site_name = "megalodon.jp"

    def _archive(self):
        """
        Archives to megalodon.jp. The website gives a 302 redirect when we
        POST to the webpage. We can't guess the link because a 1 second
        discrepancy will give an error when trying to view it.
        :return: URL of the archive, or False if an error occurred.
        """

        # Megalodon.jp sucks and errors out every single time. We'll just let
        # users archive it themselves if they want to.
        return False

        pairs = {"url": self.url}

        try:
            res = s.post("http://megalodon.jp/pc/get_simple/decide", pairs)
        except RECOVERABLE_EXC:
            return False

        if res.url == "http://megalodon.jp/pc/get_simple/decide":
            return False

        return res.url

    def resubmit_link(self):
        return "http://megalodon.jp/pc/get_simple/decide?url={}".format(self.url)


class RemovedditArchive(Archive):
    site_name = "removeddit.com"

    def _archive(self):
        return re.sub(REDDIT_PATTERN, "https://removeddit.com", self.url)


class Link:
    def __init__(self, url, title):
        log.debug("Creating Link {}".format(url))

        self.url = url
        self.fixed_url = self.fix_url(url)

        self.title = title

        if len(self.title) > (MAX_TITLE_LENGTH + 3):
            self.title = title[:MAX_TITLE_LENGTH] + "..."

        self.archives = [ArchiveOrgArchive(self.fixed_url),
                         MegalodonJPArchive(self.fixed_url)]

        if self.is_reddit():
            self.archives.append(RemovedditArchive(self.fixed_url))

        self.archives.append(ArchiveIsArchive(self.fixed_url))

    def fix_url(self, url):
        # Link on reddit
        if SUBREDDIT_OR_USER.match(url):
            url = urljoin(REDDIT_LOCATION, url)
        elif REDDIT_PATTERN.match(url):
            url = REDDIT_PATTERN.sub(REDDIT_LOCATION, url)

        return url

    def is_reddit(self):
        return self.fixed_url.startswith("http://www.reddit.com")

    def is_subreddit_or_user(self):
        return self.is_reddit() and SUBREDDIT_OR_USER.search(self.fixed_url)

    def build(self, no=1):
        archives = ", ".join(
            archive.build() for archive in self.archives if archive.archived is not None
        )

        return "{}. [{}]({}) - {}".format(no, self.title, self.url, archives)


class Post:
    def __init__(self, submission):
        self.submission = submission
        self.links = []
        self._formatted = None

        self.links.append(Link(self.submission.url, self.submission.title))

        if self.submission.selftext_html:
            self.links += self.parse_links(submission.selftext_html)

    def parse_links(self, body):
        anchors = BeautifulSoup(unescape(body)).find_all("a")
        seen_urls = []
        links = []

        for anchor in anchors:
            log.debug("Found link in text post...")
            link = Link(anchor.attrs['href'], anchor.text)

            if link.fixed_url in seen_urls or link.is_subreddit_or_user():
                continue

            seen_urls.append(link.fixed_url)
            links.append(link)

        return links

    @property
    def name(self):
        return self.submission.name

    @property
    def permalink(self):
        return self.submission.permalink

    def build_links(self):
        lines = []

        for i, link in enumerate(self.links, 1):
            lines.append(link.build(i))

        return "\n".join(lines)

    def build(self):
        self._formatted = self.build_links()

        return self._formatted

    def add_comment(self, *args, **kwargs):
        return self.submission.reply(*args, **kwargs)


class Notification:
    def __init__(self, post, header):
        self.post = post
        self.header = header
        self.reply = None
        self._formatted = None

    def notify(self):
        """
        Replies with a comment containing the archives or if there are too
        many links to fit in a comment, post a submisssion to
        /r/SnapshillBotEx and then make a comment linking to it.
        :return Nothing
        """
        log.debug("Creating notification for {}".format(self.post.name))

        self.build()

        try:
            if self.can_comment():
                comment = self.new_comment()
            else:
                comment = self.new_post()

            self.reply = comment.name
        except RECOVERABLE_EXC as e:
            log_error(e)
            return

        return comment

    def build(self):
        parts = [self.header.get(), "Snapshots:", self.post.build(), get_footer()]
        self._formatted = "\n\n".join(parts)

        return self._formatted

    def can_comment(self):
        return len(self._formatted) < MAX_COMMENT_LENGTH

    def new_post(self):
        title = "Archives for {}".format(self.post.permalink)

        post = r.subreddit(EXTENDED_POST_SUBREDDIT).submit(title, selftext=self._formatted)
        comment = post.reply(
            "The original submission can be found here:\n\n{}".format(self.post.permalink)
        )

        comment = self.post.add_comment(
            "Wow, that's a lot of links! The snapshots"
            "can be [found here.]({})\n\n{}".format(post.url, get_footer())
        )

        return comment

    def new_comment(self):
        return self.post.add_comment(self._formatted)


class Header:
    def __init__(self, settings_wiki, subreddit):
        self.subreddit = subreddit
        self.texts = []
        self._settings = r.subreddit(settings_wiki)

        try:
            content = self._get_wiki_content()
            if not content.startswith("!ignore"):
                self.texts = self._parse_quotes(content)
        except RECOVERABLE_EXC:
            pass

    def __len__(self):
        return len(self.texts)

    def get(self):
        """
        Gets a random message from the extra text or nothing if there are no
        messages.
        :return: Random message or an empty string if the length of "texts"
        is 0.
        """
        return "" if not self.texts else random.choice(self.texts)

    def _get_wiki_content(self):
        return self._settings.wiki["extxt/" + self.subreddit.lower()].content_md

    def _parse_quotes(self, quotes_str):
        return [q.strip() for q in re.split('\r\n-{3,}\r\n', quotes_str) if q.strip()]


class Snapshill:
    def __init__(self, settings_wiki, limit=25):
        self.limit = limit
        self.settings_wiki = settings_wiki
        self.headers = {}
        self._setup = False

    def run(self):
        """
        Checks through the submissions and archives and posts comments.
        """
        if not self._setup:
            raise Exception("Snapshiller not ready yet!")

        start = timeit.default_timer()
        count = 0

        submissions = r.front.new(limit=self.limit)
        post_pool = Pool(4)
        reddit_pool = Pool(1)
        notify_pool = Pool(1)

        for submission in submissions:
            post = Post(submission)

            log.debug("Found submission: {}".format(post.permalink))

            if not should_notify(post.name):
                log.debug("Skipping.")
                continue

            count += 1
            post_pool.spawn(handle_post, post, self, reddit_pool, notify_pool)

        gevent.wait()

        stop = timeit.default_timer()

        log.debug("Handled {} submissions in {} seconds".format(count, stop - start))

    def setup(self):
        """
        Logs into reddit and refreshs the header text and ignore list.
        """
        self.refresh()
        self._setup = True

    def quit(self):
        self.headers = {}
        self._setup = False

    def refresh(self):
        """
        Refreshes the header text for all subreddits, unsubscribing from them
        if the bot has been banned from them.
        """
        self.headers = {"all": Header(self.settings_wiki, "all")}

        for subreddit in r.user.subreddits():
            if subreddit.user_is_banned:
                log.debug("Banned from {}: unsubscribing!".format(subreddit))
                subreddit.unsubscribe()
                continue

            name = subreddit.display_name.lower()
            self.headers[name] = Header(self.settings_wiki, name)

    def get_header(self, subreddit):
        """
        Gets the correct Header object for this subreddit. If the one for 'all'
        is not "!ignore", then this one will always be returned.
        :param subreddit: Subreddit object to get.
        :return: Extra text object found or the one for "all" if we can't find
        it or if not empty.
        """
        all = self.headers["all"]

        if len(all):
            return all  # return 'all' one for announcements

        return self.headers.get(subreddit.display_name.lower(), all)


db = sqlite3.connect(DB_FILE)
cur = db.cursor()

if __name__ == "__main__":
    limit = int(os.environ.get("LIMIT", 25))
    wait = int(os.environ.get("WAIT", 5))
    refresh = int(os.environ.get("REFRESH", 1800))

    log.info("Starting...")
    snapshill = Snapshill("SnapshillBot", limit)
    snapshill.setup()

    log.info("Started.")
    try:
        cycles = 0
        while True:
            try:
                cycles += 1
                log.info("Running")
                snapshill.run()
                log.info("Done")
                # This will refresh by default around ~30 minutes (depending
                # on delays).
                if cycles > (refresh / wait) / 2:
                    log.info("Reloading header text and ignore list...")
                    snapshill.refresh()
                    cycles = 0
            except RECOVERABLE_EXC as e:
                log_error(e)

            time.sleep(wait)
    except KeyboardInterrupt:
        pass

    snapshill.quit()
    db.close()
    exit(0)
