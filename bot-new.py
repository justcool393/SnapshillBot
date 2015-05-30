import logging
import os
import praw
import re
import random
import requests
import sqlite3
import time
import traceback

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
DB_FILE = os.environ.get("DATABASE", "snapshill.sqlite3")
LEN_MAX = 20

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

r = praw.Reddit(USER_AGENT)
me = None


def get_footer():
    return "*^(I am a bot.) ^\([*Info*]({info}) ^/ ^[*Contact*]({" \
           "contact}))*".format(info=INFO, contact=CONTACT)


def should_notify(s):
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


def archive(url, archiveis):
    if archiveis:
        pairs = {"url": url}
        try:
            res = requests.post("https://archive.is/submit/", pairs)
        except RECOVERABLE_EXC:
            return False
    else:
        try:
            requests.get("https://web.archive.org/save/" + url)
            time.sleep(500)  # archive.org can't follow reddit API rules
        except RECOVERABLE_EXC:
            return False
        date = time.strftime(ARCHIVE_ORG_FORMAT, time.gmtime())
        return "https://web.archive.org/" + date + "/" + url
    return get_archive_link(res.text)



def fix_url(url):
    if url.startswith("/r/") or url.startswith("/u/"):
        url = "https://www.reddit.com" + url
    return re.sub("https?://(([A-z]{2})(-[A-z]{2})?|beta|i|m|pay)"
                  "\.?reddit\.com", "https://www.reddit.com", url)


def log_error(e):
    log.error("Unexpected {}:\n{}".format(e.__class__.__name__,
                                          traceback.format_exc()))


class Archive:

    def __init__(self, url, text, archiveis=None, archiveorg=None):
        if archiveis is None:
            archiveis = archive(url, True)
        if archiveorg is None:
            archiveorg = archive(url, False)
        self.url = url
        self.text = (text[:LEN_MAX] + "...") if len(text) > LEN_MAX else text
        self.archiveis = archiveis
        self.archiveorg = archiveorg


class Notification:

    def __init__(self, post, ext, links):
        self.post = post
        self.ext = ext
        self.links = links

    def should_notify(self):
        cur.execute("SELECT * FROM links WHERE id=?", (self.post.name,))
        return False if cur.fetchone() else should_notify(self.post)

    def notify(self):
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
        format = "{count}. {text} - [{aisnum}]({ais}), [{aorgnum}]({aorg})"
        for l in self.links:
            aisnum = "1"
            aorgnum = "2"
            if not l.archiveis:
                aisnum = "Error"
                l.archiveis = create_archive_link(l.url, True)

            if not l.archiveorg:
                aorgnum = "Error"
                l.archiveorg = create_archive_link(l.url, False)

            parts.append(format.format(count=str(count), text=l.text,
                                       aisnum=aisnum, aorgnum=aorgnum,
                                       ais=l.archiveis, aorg=l.archiveorg))
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
            # Your crap posts aren't worth wasting precious CPU cycles and
            # archive.is and archive.org's bandwith. HAIL ELLEN PAO
            if submission.author and submission.author.name == "PoliticBot":
                continue

            archives = [Archive(submission.url, "*This Post*")]
            if submission.is_self and submission.selftext_html is not None:
                log.debug("Found text post...")
                soup = BeautifulSoup(unescape(submission.selftext_html))
                for anchor in soup.find_all('a'):
                    log.debug("Found link in text post...")
                    url = fix_url(anchor['href'])
                    archives.append(Archive(url, anchor.contents[0]))
                if len(archives) == 1:
                    continue
            n = Notification(submission, self._get_ext(submission.subreddit),
                             archives)
            if n.should_notify():
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
    me = r.user
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