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
from praw.helpers import flatten_tree

from praw.errors import APIException, ClientException, HTTPException

USER_AGENT = "Archives to archive.is and archive.org (/r/SnapshillBot) v1.3"
INFO = "/r/SnapshillBot"
CONTACT = "/message/compose?to=\/r\/SnapshillBot"
ARCHIVE_ORG_FORMAT = "%Y%m%d%H%M%S"
MEGALODON_JP_FORMAT = "%Y-%m%d-%H%M-%S"
DB_FILE = os.environ.get("DATABASE", "snapshill.sqlite3")
LEN_MAX = 35
REDDIT_API_WAIT = 2
WARN_TIME = 300 # warn after spending 5 minutes on a post
REDDIT_PATTERN = re.compile("https?://(([A-z]{2})(-[A-z]{2})"
                            "?|beta|i|m|pay|ssl|www)\.?reddit\.com")
SUBREDDIT_OR_USER = re.compile("/(u|user|r)/[^\/]+/?$")
# we have to do some manual ratelimiting because we are tunnelling through
# some other websites.

RECOVERABLE_EXC = (APIException,
                   ClientException,
                   HTTPException)


loglevel = logging.DEBUG if os.environ.get("DEBUG") == "true" else logging.INFO

logging.basicConfig(level=loglevel,
                    format="[%(asctime)s] [%(levelname)s] %(message)s")

log = logging.getLogger("snapshill")
logging.getLogger("requests").setLevel(logging.WARNING)
warnings.simplefilter("ignore")  # Ignore ResourceWarnings (because screw them)

r = praw.Reddit(USER_AGENT)
ignorelist = set()


def get_footer():
    return "*^(I am a bot.) ^\([*Info*]({info}) ^/ ^[*Contact*]({" \
           "contact}))*".format(info=INFO, contact=CONTACT)


def should_notify(submission):
    """
    Looks for other snapshot bot comments in the comment chain and doesn't
    post if they do.
    :param submission: Submission to check
    :return: If we should comment or not
    """
    cur.execute("SELECT * FROM links WHERE id=?", (submission.name,))
    if cur.fetchone():
        return False
    submission.replace_more_comments()
    for comment in flatten_tree(submission.comments):
        if comment.author and comment.author.name in ignorelist:
            return False
    return True


def ratelimit(url):
    if len(re.findall(REDDIT_PATTERN, url)) == 0:
        return
    time.sleep(REDDIT_API_WAIT)


def refresh_ignore_list():
    ignorelist.clear()
    ignorelist.add(r.user.name)
    for friend in r.user.get_friends():
        ignorelist.add(friend.name)


def fix_url(url):
    """
    Change language code links, mobile links and beta links, SSL links and
    username/subreddit mentions
    :param url: URL to change.
    :return: Returns a fixed URL
    """
    if url.startswith("r/") or url.startswith("u/"):
        url = "http://www.reddit.com" + url
    return re.sub(REDDIT_PATTERN, "http://www.reddit.com", url)


def skip_url(url):
    """
    Skip naked username mentions and subreddit links.
    """
    if REDDIT_PATTERN.match(url) and SUBREDDIT_OR_USER.search(url):
        return True

    return False


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
            res = requests.post("https://archive.is/submit/", pairs, verify=False)
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
        if res.url == "http://megalodon.jp/pc/get_simple/decide":
            return False
        return res.url


class GoldfishArchive:

    def __init__(self, url):
        self.url = url
        self.archived = re.sub(REDDIT_PATTERN, "http://r.go1dfish.me/", url)
        self.error_link = "http://r.go1dfish.me/"

class ArchiveContainer:

    def __init__(self, url, text):
        log.debug("Creating ArchiveContainer")
        self.url = url
        self.text = (text[:LEN_MAX] + "...") if len(text) > LEN_MAX else text
        self.archives = [ArchiveIsArchive(url), ArchiveOrgArchive(url),
                         MegalodonJPArchive(url)]
        if re.match(REDDIT_PATTERN, url):
            self.archives.append(GoldfishArchive(url))


class Notification:

    def __init__(self, post, header, links):
        self.post = post
        self.header = header
        self.links = links

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
                comment = self.post.add_comment("Wow, that's a lot of links! The "
                                          "snapshots can be [found here.](" +
                                          submission.url + ")\n\n" + get_footer())
                log.info("Posted a comment and new submission")
            else:
                comment = self.post.add_comment(comment)
        except RECOVERABLE_EXC as e:
            log_error(e)
            return
        cur.execute("INSERT INTO links (id, reply) VALUES (?, ?)",
                    (self.post.name, comment.name))

    def _build(self):
        parts = [self.header.get(), "Snapshots:"]
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
            parts.append("{}. {} - {}".format(count, l.text, ", ".join(subparts)))
            count += 1

        parts.append(get_footer())
        return "\n\n".join(parts)


class Header:

    def __init__(self, settings_wiki, subreddit):
        self.subreddit = subreddit
        settings = r.get_subreddit(settings_wiki)
        self.texts = []

        try:
            content = settings.get_wiki_page("extxt/" + subreddit.lower()).content_md
            if not content.startswith("!ignore"):
                self.texts = content.split("\r\n----\r\n")
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


class Snapshill:

    def __init__(self, username, password, settings_wiki, limit=25):
        self.username = username
        self.password = password
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

        submissions = r.get_new(limit=self.limit)

        for submission in submissions:
            debugTime = time.time()
            warned = False

            log.debug("Found submission.\n" + submission.permalink)

            if not should_notify(submission):
                log.debug("Skipping.")
                continue

            archives = [ArchiveContainer(fix_url(submission.url),
                                         "*This Post*")]
            if submission.is_self and submission.selftext_html is not None:
                log.debug("Found text post...")

                links = BeautifulSoup(unescape(
                    submission.selftext_html)).find_all("a")

                if not len(links):
                    continue

                finishedURLs = []

                for anchor in links:
                    if time.time() > debugTime + WARN_TIME and not warned:
                        log.warn("Spent over {} seconds on post (ID: {})".format(
                            WARN_TIME, submission.name))

                        warned = True

                    log.debug("Found link in text post...")

                    url = fix_url(anchor['href'])

                    if skip_url(url):
                        continue

                    if url in finishedURLs:
                        continue #skip for sanity

                    archives.append(ArchiveContainer(url, anchor.contents[0]))
                    finishedURLs.append(url)
                    ratelimit(url)

            Notification(submission, self._get_header(submission.subreddit),
                         archives).notify()
            db.commit()

    def setup(self):
        """
        Logs into reddit and refreshs the header text and ignore list.
        """
        self._login()
        self.refresh_headers()
        refresh_ignore_list()
        self._setup = True

    def quit(self):
        self.headers = {}
        self._setup = False

    def refresh_headers(self):
        """
        Refreshes the header text for all subreddits.
        """
        self.headers = {"all": Header(self.settings_wiki, "all")}
        for subreddit in r.get_my_subreddits():
            name = subreddit.display_name.lower()
            self.headers[name] = Header(self.settings_wiki, name)

    def _login(self):
        r.login(self.username, self.password)

    def _get_header(self, subreddit):
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
    username = os.environ.get("REDDIT_USER")
    password = os.environ.get("REDDIT_PASS")
    limit = int(os.environ.get("LIMIT", 25))
    wait = int(os.environ.get("WAIT", 5))
    refresh = int(os.environ.get("REFRESH", 1800))

    log.info("Starting...")
    snapshill = Snapshill(username, password, "SnapshillBot", limit)
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
                    refresh_ignore_list()
                    snapshill.refresh_headers()
                    cycles = 0
            except RECOVERABLE_EXC as e:
                log_error(e)

            time.sleep(wait)
    except KeyboardInterrupt:
        pass
    snapshill.quit()
    db.close()
    exit(0)
