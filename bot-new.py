import logging
import os
import praw
import re
import random
import requests
import sqlite3
import time
import traceback
import warnings

from bs4 import BeautifulSoup
from html.parser import unescape
from urllib.parse import urlencode

# Requests' exceptions live in .exceptions and are called errors.
from requests.exceptions import ConnectionError, HTTPError
# Praw's exceptions live in .errors and are called exceptions.
from praw.errors import APIException, ClientException, RateLimitExceeded, \
    InvalidCaptcha

USER_AGENT = "Archives to archive.is and archive.org (/u/justcool393) v1.2"
INFO = "/r/SnapshillBot"
CONTACT = "/message/compose?to=\/r\/SnapshillBot"
ARCHIVE_ORG_FORMAT = "%Y%m%d%H%M%S"
MEGALODON_JP_FORMAT = "%Y-%m%d-%H%M-%S"
DB_FILE = os.environ.get("DATABASE", "snapshill.sqlite3")
LEN_MAX = 35
REDDIT_API_WAIT = 2
REDDIT_PATTERN = re.compile("https?://(([A-z]{2})(-[A-z]{2})"
                            "?|beta|i|m|pay|ssl|www)\.?reddit\.com")
# we have to do some manual ratelimiting because we are tunnelling through
# some other websites.

RECOVERABLE_EXC = (ConnectionError,
                   HTTPError,
                   APIException,
                   ClientException,
                   RateLimitExceeded,
                   InvalidCaptcha)

loglevel = logging.INFO

logging.basicConfig(level=loglevel,
                    format="[%(asctime)s] [%(levelname)s] %(message)s")

log = logging.getLogger("snapshill")
logging.getLogger("requests").setLevel(logging.WARNING)
warnings.simplefilter("ignore")  # Ignore ResourceWarnings (because screw them)

r = praw.Reddit(USER_AGENT)
me = None


def get_footer():
    return "*^(I am a bot.) ^\([*Info*]({info}) ^/ ^[*Contact*]({" \
           "contact}))*".format(info=INFO, contact=CONTACT)


def should_notify(s):
    """
    Looks for other snapshot bot comments in the comment chain and doesn't
    post if they do.
    :param s: Submission to check
    :return: If we should comment or not
    """

    s.replace_more_comments()
    flat_comments = praw.helpers.flatten_tree(s.comments)
    for c in flat_comments:
        if c.author and (c.author == me or c.author in me.get_friends()):
            return False
    return True


def get_archive_link(data):
    a = re.findall("http[s]?://archive.is/[0-z]{1,6}", data)
    if len(a) < 1:
        return False
    return a[0]


def create_archive_link(url, archiveis):
    if archiveis:
        pairs = {"url": url, "run": '1'}
        return "https://archive.is/?" + urlencode(pairs)
    return "https://web.archive.org/save/" + url


def ratelimit(url):
    if len(re.findall(REDDIT_PATTERN, url)) == 0:
        return
    time.sleep(REDDIT_API_WAIT)

def fix_url(url):
    """
    Change language code links, mobile links and beta links, SSL links and
    username/subreddit mentions
    :param url: URL to change.
    :return: Returns a fixed URL
    """
    if url.startswith("/r/") or url.startswith("/u/"):
        url = "https://www.reddit.com" + url
    return re.sub(REDDIT_PATTERN, "https://www.reddit.com", url)


def log_error(e):
    log.error("Unexpected {}:\n{}".format(e.__class__.__name__,
                                          traceback.format_exc()))


class ArchiveIsArchive:

    def __init__(self, url):
        self.url = url
        self.archived = self.archive()
        pairs = {"url": self.url, "run": 1}
        self.error_link = "https://archive.is/?" + urlencode(pairs)

    def archive(self):
        """
        Archives to archive.is. Returns a 200, and we have to find the
        JavaScript redirect through a regex in the response text.
        :return: URL of the archive or False if an error occurred
        """
        pairs = {"url": self.url}
        try:
            res = requests.post("https://archive.is/submit/", pairs)
        except RECOVERABLE_EXC:
            return False
        found = re.findall("http[s]?://archive.is/[0-z]{1,6}", res.text)
        if len(found) < 1:
            return False
        return found[0]


class ArchiveOrgArchive:

    def __init__(self, url):
        self.url = url
        self.archived = self.archive()
        self.error_link = "https://web.archive.org/save/" + self.url

    def archive(self):
        """
        Archives to archive.org. The website gives a 403 Forbidden when the
        archive cannot be generated (because it follows robots.txt rules)
        :return: URL of the archive, False if an error occurred, or None if
        we cannot archive this page.
        """
        try:
            requests.get("https://web.archive.org/save/" + self.url)
        except RECOVERABLE_EXC as e:
            if isinstance(e, HTTPError) and e.status_code == 403:
                return None
            return False
        date = time.strftime(ARCHIVE_ORG_FORMAT, time.gmtime())
        ratelimit(self.url)
        return "https://web.archive.org/" + date + "/" + self.url


class MegalodonJPArchive:

    def __init__(self, url):
        self.url = url
        self.archived = self.archive()
        self.error_link = "http://megalodon.jp/"

    def archive(self):
        """
        Archives to megalodon.jp. The website gives a 302 redirect when we
        POST to the webpage. We can't guess the link because a 1 second
        discrepancy will give an error when trying to view it.
        :return: URL of the archive, or False if an error occurred.
        """
        pairs = {"url": self.url}
        try:
            res = requests.post("http://megalodon.jp/pc/get_simple/decide",
                                pairs)
        except RECOVERABLE_EXC:
            return False
        ratelimit(self.url)
        if res.url == "http://megalodon.jp/pc/get_simple/decide":
            return False
        return res.url


class ArchiveContainer:

    def __init__(self, url, text):
        log.debug("Creating ArchiveContainer")
        self.url = url
        self.text = (text[:LEN_MAX] + "...") if len(text) > LEN_MAX else text
        self.archives = [ArchiveIsArchive(url), ArchiveOrgArchive(url),
                         MegalodonJPArchive(url)]


class Notification:

    def __init__(self, post, ext, links):
        self.post = post
        self.ext = ext
        self.links = links

    def should_notify(self):
        """
        Queries the database to see if we should post, and then checks for
        other bot posts.
        :return: True if we should post, false otherwise.
        """
        cur.execute("SELECT * FROM links WHERE id=?", (self.post.name,))
        return False if cur.fetchone() else should_notify(self.post)

    def notify(self):
        """
        Replies with a comment containing the archives or if there are too
        many links to fit in a comment, post a submisssion to
        /r/SnapshillBotEx and then make a comment linking to it.
        :return Nothing
        """
        try:
            comment = self._build()
            if len(comment) > 9999:
                link = self.post.permalink
                submission = r.submit("SnapshillBotEx", "Archives for " + link,
                                      text=comment[:39999],
                                      raise_captcha_exception=True)
                submission.add_comment("The original submission can be found "
                                       "here:\n\n" + link)
                c = self.post.add_comment("Wow, that's a lot of links! The "
                                          "snapshots can be [found here.](" +
                                          submission.url + ")\n\n" + get_footer())
                log.info("Posted a comment and new submission")
            else:
                c = self.post.add_comment(comment)
        except RECOVERABLE_EXC as e:
            log_error(e)
            return
        cur.execute("INSERT INTO links (id, reply) VALUES (?, ?)",
                    (self.post.name, c.name))

    def _build(self):
        parts = [self.ext.get(), "Snapshots:"]
        count = 1
        format = "[{num}]({archive})"
        for l in self.links:
            subparts = []
            subcount = 1
            log.debug("Found link")
            for archive in l.archives:
                if archive.archived is None:
                    continue
                archive_link = archive.archived
                if not archive_link:
                    log.debug("Not found, using error link")
                    archive_link = archive.error_link + " \"error " \
                                                        "auto-archiving; " \
                                                        "click to submit it!\""
                    subparts.append(format.format(num="Error",
                                                  archive=archive_link))
                    continue
                log.debug("Found archive")
                subparts.append(format.format(num=str(subcount),
                                              archive=archive_link))
                subcount += 1
            parts.append(str(count) + ". " + l.text + " - " +
                         ", ".join(subparts))
            count += 1

        parts.append(get_footer())
        return "\n\n".join(parts)


class ExtendedText:

    def __init__(self, wikisr, subreddit):
        self.subreddit = subreddit
        s = r.get_subreddit(wikisr)
        try:
            c = s.get_wiki_page("extxt/" + subreddit.lower()).content_md
            if c.startswith("!ignore"):
                self.all = []
            else:
                self.all = c.split("\r\n----\r\n")
        except RECOVERABLE_EXC:
            self.all = []

    def __len__(self):
        return self.all.__len__()

    def get(self):
        """
        Gets a random message from the extra text or nothing if there are no 
        messages.
        :return: Random message or an empty string if the length of "all" is 0.
        """
        if len(self.all) == 0: return ""
        return random.choice(self.all)


class Snapshill:

    def __init__(self, username, password, wikisr, limit=25):
        self.username = username
        self.password = password
        self.limit = limit
        self.wikisr = wikisr
        self.extxt = []
        self._setup = False

    def run(self):
        """
        Checks through the submissions and archives and posts comments.
        """
        if not self._setup:
            raise Exception("Snapshiller not ready yet!")

        submissions = r.get_new(limit=self.limit)

        for submission in submissions:
            # Your crap posts aren't worth wasting precious CPU cycles and
            # archive.is and archive.org's bandwith. HAIL ELLEN PAO
            if submission.author and submission.author.name == "PoliticBot":
                continue
            log.debug("Found submission.\n" + submission.permalink)
            archives = [ArchiveContainer(fix_url(submission.url),
                                         "*This Post*")]
            if submission.is_self and submission.selftext_html is not None:
                log.debug("Found text post...")
                soup = BeautifulSoup(unescape(submission.selftext_html))
                for anchor in soup.find_all('a'):
                    log.debug("Found link in text post...")
                    url = fix_url(anchor['href'])
                    archives.append(ArchiveContainer(url, anchor.contents[0]))
                if len(archives) == 1:
                    continue
            n = Notification(submission, self._get_ext(submission.subreddit),
                             archives)
            if n.should_notify():
                n.notify()
                db.commit()

    def setup(self):
        """
        Logs into reddit and refreshs the extra text.
        """
        self._login()
        self.refresh_extxt()
        self._setup = True

    def quit(self):
        self.extxt = []
        self._setup = False

    def refresh_extxt(self):
        """
        Refreshes the header text for all subreddits.
        """
        self.extxt = [ExtendedText(self.wikisr, "all")]
        for s in r.get_my_subreddits():
            self.extxt.append(ExtendedText(self.wikisr, s.display_name))

    def _login(self):
        r.login(self.username, self.password)

    def _get_ext(self, subreddit):
        """
        Gets the correct ExtendedText object for this subreddit. If the one 
        for 'all' is not "!ignore", then this one will always be returned.
        :param subreddit: Subreddit object to get.
        :return: Extra text object found or the one for "all" if we can't 
        find it or if not empty.
        """
        if len(self.extxt[0]) != 0:
            return self.extxt[0]  # return 'all' one for announcements

        for ex in self.extxt:
            if ex.subreddit.lower() == subreddit.display_name.lower():
                return ex

        return self.extxt[0]


db = sqlite3.connect(DB_FILE)
cur = db.cursor()

if __name__ == "__main__":
    username = os.environ.get("REDDIT_USER")
    password = os.environ.get("REDDIT_PASS")
    limit = int(os.environ.get("LIMIT", 25))
    wait = int(os.environ.get("WAIT", 5))
    refresh = int(os.environ.get("REFRESH", 1800))

    log.info("Starting...")
    b = Snapshill(username, password, "SnapshillBot", limit)
    b.setup()
    me = r.user
    log.info("Started.")
    try:
        cycles = 0
        while True:
            try:
                cycles += 1
                log.debug("Running")
                b.run()
                log.debug("Done")
                # This will refresh by default around ~30 minutes (depending
                # on delays).
                if cycles > (refresh / wait) / 2:
                    log.info("Reloading header text...")
                    b.refresh_extxt()
                    cycles = 0
            except RECOVERABLE_EXC as e:
                log_error(e)

            time.sleep(wait)
    except KeyboardInterrupt:
        pass
    b.quit()
    db.close()
    exit(0)