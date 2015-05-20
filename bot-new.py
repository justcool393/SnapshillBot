import logging
import os
import praw
import re
import random
import sqlite3
import time
import traceback
import urllib.error

from bs4 import BeautifulSoup
from html.parser import unescape
from urllib.request import urlopen
from urllib.parse import urlencode

# Requests' exceptions live in .exceptions and are called errors.
from requests.exceptions import ConnectionError, HTTPError
# Praw's exceptions live in .errors and are called exceptions.
from praw.errors import APIException, ClientException, RateLimitExceeded

USER_AGENT = "Archives to archive.is (/u/justcool393) v1.1"
REDDIT_DOMAIN = "api.reddit.com"
INFO = "/r/SnapshillBot"
CONTACT = "/message/compose?to=\/r\/SnapshillBot"
ARCHIVE_BOTS = ["snapshillbot", "ttumblrbots"]
DB_FILE = os.environ.get("DATABASE", "snapshill.sqlite3")

RECOVERABLE_EXC = (ConnectionError,
                   HTTPError,
                   APIException,
                   ClientException,
                   RateLimitExceeded)

loglevel = logging.INFO

logging.basicConfig(level=loglevel,
                    format="[%(asctime)s] [%(levelname)s] %(message)s")

log = logging.getLogger("snapshill")
logging.getLogger("requests").setLevel(logging.WARNING)

r = praw.Reddit(USER_AGENT, domain=REDDIT_DOMAIN)


def get_footer():
    return "*^(I am a bot.) ^\([*Info*]({info}) ^/ ^[*Contact*]({" \
           "contact}))*".format(info=INFO, contact=CONTACT)


def should_notify(s):
    s.replace_more_comments()
    flat_comments = praw.helpers.flatten_tree(s.comments)
    for c in flat_comments:
        if c.author and c.author.name.lower() in ARCHIVE_BOTS:
            return False
    return True


def get_archive_link(data):
    a = re.findall("http[s]?://archive.is/[0-z]{1,6}", data)
    if len(a) < 1:
        return False
    return a[0]


def create_archive_link(url):
    pairs = {"url": url, "run": '1'}
    return "https://archive.is/?" + urlencode(pairs)


def archive(url):
    pairs = {"url": url}
    try:
        res = urlopen("https://archive.is/submit/", urlencode(pairs).encode(
            'utf-8'))
    except urllib.error.HTTPError:
        return False
    return get_archive_link(res.read().decode('utf-8'))


def fix_url(url):
    if url.startswith("/r/") or url.startswith("/u/"):
        url = "https://www.reddit.com" + url
    return url


def log_error(e):
    log.error("Unexpected {}:\n{}".format(e.__class__.__name__,
                                          traceback.format_exc()))


class Notification:

    def __init__(self, post, ext, links, originals):
        self.post = post
        self.ext = ext
        self.links = links
        self.originals = originals

    def should_notify(self):
        cur.execute("SELECT * FROM links WHERE id=?", (self.post.name,))
        return False if cur.fetchone() else True

    def notify(self):
        try:
            c = self.post.add_comment(self._build())
        except RECOVERABLE_EXC as e:
            log_error(e)
            return
        cur.execute("INSERT INTO links (id, reply) VALUES (?, ?)",
                    (self.post.name, c.name))

    def _build(self):
        parts = [self.ext.get(), "Snapshots:"]
        count = 1
        for l in self.links:
            msg = "Link " + str(count)
            if self.post.is_self:
                if count == 1:
                    msg = "*This Post*"
                else:
                    msg = "Link " + str(count - 1)
            if l is False:
                parts.append("* *Error archiving link " + str(count)
                             + " ([archive manually?]("
                             + create_archive_link(self.originals[l - 1])
                             + "))*")
            else:
                parts.append("* [" + msg + "](" + l + ")")
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
                self.extxt = []
            else:
                self.extxt = c.split("\r\n----\r\n")
        except RECOVERABLE_EXC:
            self.extxt = []

    def get(self):
        if len(self.extxt) == 0: return ""
        return random.choice(self.extxt)


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
        TODO: Add comments
        """
        if not self._setup:
            raise Exception("Snapshiller not ready yet!")

        submissions = r.get_new(limit=self.limit)

        for submission in submissions:
            archives = [archive(submission.url)]
            originals = [submission.url]
            if submission.is_self and submission.selftext_html is not None:
                soup = BeautifulSoup(unescape(submission.selftext_html))
                for anchor in soup.find_all('a'):
                    url = fix_url(anchor['href'])
                    archives.append(archive(url))
                    originals.append(url)
            n = Notification(submission, self._get_ext(submission.subreddit),
                             archives, originals)
            if should_notify(submission) and n.should_notify():
                n.notify()
            db.commit()

    def setup(self):
        self._login()
        self.refresh_extxt()
        self._setup = True

    def quit(self):
        self.extxt = []
        self._setup = False

    def refresh_extxt(self):
        self.extxt = [ExtendedText(self.wikisr, "all")]
        for s in r.get_my_subreddits():
            self.extxt.append(ExtendedText(self.wikisr, s.display_name))

    def _login(self):
        r.login(self.username, self.password)

    def _get_ext(self, subreddit):
        if len(self.extxt[0].extxt) != 0:
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

    b = Snapshill(username, password, "SnapshillBot", limit)
    b.setup()
    try:
        cycles = 0
        while True:
            try:
                cycles += 1
                b.run()
                # This will refresh by default around ~30 minutes (depending
                # on delays).
                if cycles > (refresh / wait) / 2:
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