import ftplib
import logging
import os
import praw
import re
import random
import sqlite3
import time
import traceback

from bs4 import BeautifulSoup
from html.parser import unescape
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.parse import urlparse

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
logging.getLogger("requests").setLevel(loglevel)

r = praw.Reddit(USER_AGENT, domain=REDDIT_DOMAIN)

def get_footer():
    return "*^(I am a bot.) ^\([*Info*]({info}) ^/ ^[*Contact*]({" \
           "contact}))*".format(info=INFO, contact=CONTACT)


def should_notify(s):
    flat_comments = praw.helpers.flatten_tree(s.comments)
    for c in flat_comments:
        if c.author and c.author.name.lower() in ARCHIVE_BOTS:
            return False
    return True


def get_archive_link(data):
    return re.findall("http[s]?://archive.is/[0-z]{1,6}", data)[0]


def archive(url):
    pairs = {"url": url}
    res = urlopen("https://archive.is/submit/", urlencode(pairs).encode(
        'ascii'))
    return get_archive_link(res.read().decode('ascii'))


def log_error(e):
    log.error("Unexpected {}:\n{}".format(e.__class__.__name__,
                                          traceback.format_exc()))
class FTPSaver:

    def __init__(self, file, folder, server, user, ftppass):
        self.file = file
        self.folder = folder
        self.server = server
        self.user = user
        self.ftppass = ftppass

    def create_session(self):
        session = ftplib.FTP(self.server, self.user, self.ftppass)
        session.cwd(self.folder)
        return session

    def upload(self):
        session = self.create_session()
        f = open(self.file, 'rb')
        session.storbinary("STOR " + self.file, f)
        f.close()
        session.quit()

    def download(self):
        session = self.create_session()
        session.retrbinary("RETR " + self.file, open(self.file, 'wb').write)
        session.quit()


class Notification:

    def __init__(self, post, ext, links):
        self.post = post
        self.ext = ext
        self.links = links

    def should_notify(self):
        cur.execute("SELECT * FROM links WHERE id=?", (self.post.name,))
        return False if cur.fetchone() else True

    def notify(self):
        c = self.post.add_comment(self._build())
        cur.execute("INSERT INTO links (id, reply) VALUES (?, ?)",
                    (self.post.name, c.name))

    def _build(self):
        parts = [self.ext.get(), "Snapshots:"]
        count = 1
        for l in self.links:
            msg = "Link " + str(count)
            if count == 1 and self.post.is_self:
                msg = "*This Post*"
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
                self.extxt = [""]
            else:
                self.extxt = c.split("\r\n----\r\n")
        except RECOVERABLE_EXC:
            self.extxt = [""]

    def get(self):
        return random.choice(self.extxt)


class Snapshill:

    def __init__(self, username, password, wikisr, limit=25):
        self.username = username
        self.password = password
        self.limit = limit
        self.wikisr = wikisr
        self.extxt = [ExtendedText(wikisr, "all")]
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
            if submission.is_self and submission.selftext_html is not None:
                soup = BeautifulSoup(unescape(submission.selftext_html))
                for anchor in soup.find_all('a'):
                    url = anchor['href']
                    archives.append(archive(url))
            n = Notification(submission, self._get_ext(submission.subreddit),
                             [archive(submission.url)])
            if n.should_notify() and should_notify(submission):
                n.notify()

    def setup(self):
        self._login()
        for s in r.get_my_subreddits():
            self.extxt.append(ExtendedText(self.wikisr, s.display_name))

        self._setup = True

    def _login(self):
        r.login(self.username, self.password)

    def _get_ext(self, subreddit):
        for e in self.extxt:
            if e.subreddit.lower() == subreddit.display_name.lower():
                return e
        return self.extxt[0]


u = FTPSaver(DB_FILE, "htdocs", os.environ.get("FTP_SRV"),
             os.environ.get("FTP_USER"), os.environ.get("FTP_PASS"))

u.download() # Download

db = sqlite3.connect(DB_FILE)
cur = db.cursor()

if __name__ == "__main__":
    username = os.environ.get("REDDIT_USER")
    password = os.environ.get("REDDIT_PASS")
    limit = int(os.environ.get("LIMIT", 25))
    wait = int(os.environ.get("WAIT", 60*3))
    save_cycle = int(os.environ.get("SAVE_CYCLE", 20))

    b = Snapshill(username, password, "SnapshillBot", limit)
    b.setup()
    try:
        cycles = 0
        while True:
            try:
                b.run()
            except RECOVERABLE_EXC as e:
                log_error(e)

            time.sleep(wait)
            cycles += 1
            if cycles >= save_cycle:
                u.upload()
                cycles = 0
    except KeyboardInterrupt:
        pass
    b.quit()
    db.close()
    u.upload()
    exit(0)