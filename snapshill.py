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

from praw.exceptions import APIException, ClientException, PRAWException
from prawcore.exceptions import PrawcoreException
from requests.exceptions import ConnectionError

USER_AGENT = "Archives to archive.is and archive.org (/r/SnapshillBot) v1.4"
INFO = "/r/SnapshillBot"
CONTACT = "/message/compose?to=\/r\/SnapshillBot"
ARCHIVE_ORG_FORMAT = "%Y%m%d%H%M%S"
MEGALODON_JP_FORMAT = "%Y-%m%d-%H%M-%S"
DB_FILE = os.environ.get("DATABASE", "snapshill.sqlite3")
LEN_MAX = 35
REDDIT_API_WAIT = 2
WARN_TIME = 300  # warn after spending 5 minutes on a post
REDDIT_PATTERN = re.compile(
    "https?://(([A-z]{2})(-[A-z]{2})" "?|beta|i|m|pay|ssl|www|old|new|alpha)\.?reddit\.com"
)
SUBREDDIT_OR_USER = re.compile("/(u|user|r)/[^\/]+/?$")
# we have to do some manual ratelimiting because we are tunnelling through
# some other websites.

RECOVERABLE_EXC = (
    APIException,
    ClientException,
    PRAWException,
    PrawcoreException,
    ConnectionError,
)


loglevel = logging.DEBUG if os.environ.get("DEBUG") == "true" else logging.INFO
TESTING = os.environ.get("TEST") == "true"

logging.basicConfig(level=loglevel, format="[%(asctime)s] [%(levelname)s] %(message)s")

log = logging.getLogger("snapshill")
logging.getLogger("requests").setLevel(loglevel)
warnings.simplefilter("ignore")  # Ignore ResourceWarnings (because screw them)


def get_footer():
    return "\n\n*I am just a simple bot, __not__ a moderator of this subreddit* | [*bot subreddit*]({info}) | [*contact the maintainers*]({contact})".format(
        info=INFO, contact=CONTACT
    )


def should_notify(submission):
    """
    Looks if we have seen this link before.
    :param submission: Submission to check
    :return: If we should comment or not
    """
    cur.execute("SELECT * FROM links WHERE id=?", (submission.name,))
    return not cur.fetchone()


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
    if url.startswith("r/") or url.startswith("u/"):
        url = "http://old.reddit.com/" + url
    if url.startswith("/r/") or url.startswith("/u/"):
        url = "http://old.reddit.com" + url
    return re.sub(REDDIT_PATTERN, "http://old.reddit.com", url)


def skip_url(url):
    """
    Skip naked username mentions and subreddit links.
    """
    return REDDIT_PATTERN.match(url) and SUBREDDIT_OR_USER.search(url)


def log_error(e):
    log.error("Unexpected {}:\n{}".format(e.__class__.__name__, traceback.format_exc()))


class NameMixin:
    site_name = None

    @property
    def name(self):
        if self.archived:
            return self.site_name
        else:
            return "_{}\*_".format(self.site_name)


class ArchiveIsArchive(NameMixin):
    site_name = "archive.today"

    def __init__(self, url):
        self.url = url
        self.archived = self.archive()
        pairs = {"url": self.url, "run": 1}
        self.error_link = "https://archive.today/?" + urlencode(pairs)

    def archive(self):
        """
        Archives to archive.is. Returns a 200, and we have to find the
        JavaScript redirect through a regex in the response text.
        :return: URL of the archive or False if an error occurred
        """
        pairs = {"url": self.url}

        try:
            res = requests.post("https://archive.today/submit/", pairs, verify=False)
        except RECOVERABLE_EXC:
            return False

        # Note; findall returns a list of tuples [('url', 'tld')]
        found = re.findall(
            "(http[s]?://archive.(fo|vn|today|is|li|md|ph)/[0-z]{1,6})", res.text
        )

        if len(found) < 1:
            return False

        return found[0][0]


class ArchiveOrgArchive(NameMixin):
    site_name = "archive.org"

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


class MegalodonJPArchive(NameMixin):
    site_name = "megalodon.jp"

    def __init__(self, url):
        self.url = url
        self.archived = self.archive()
        self.error_link = "http://megalodon.jp/pc/get_simple/decide?url={}".format(
            self.url
        )

    def archive(self):
        """
        Archives to megalodon.jp. The website gives a 302 redirect when we
        POST to the webpage. We can't guess the link because a 1 second
        discrepancy will give an error when trying to view it.
        :return: URL of the archive, or False if an error occurred.
        """
        pairs = {"url": self.url}
        try:
            res = requests.post("http://megalodon.jp/pc/get_simple/decide", pairs)
        except RECOVERABLE_EXC:
            return False
        if res.url == "http://megalodon.jp/pc/get_simple/decide":
            return False
        return res.url


class GoldfishArchive(NameMixin):
    site_name = "snew.github.io"

    def __init__(self, url):
        self.url = url
        self.archived = re.sub(REDDIT_PATTERN, "https://snew.github.io", url)
        self.error_link = "https://snew.github.io/"


class RemovedditArchive(NameMixin):
    site_name = "removeddit.com"

    def __init__(self, url):
        self.url = url
        self.archived = re.sub(REDDIT_PATTERN, "https://www.removeddit.com", url)
        self.error_link = "https://www.removeddit.com/"


class ArchiveContainer:
    def __init__(self, url, text):
        log.debug("Creating ArchiveContainer")
        self.url = url
        self.text = (text[:LEN_MAX] + "...") if len(text) > LEN_MAX else text
        self.archives = [ArchiveOrgArchive(url), ArchiveIsArchive(url)]

        if re.match(REDDIT_PATTERN, url):
            self.archives.append(RemovedditArchive(url))


class Notification:
    def __init__(self, reddit, post, header, links):
        self.reddit = reddit
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
            if TESTING:
                print(comment)
                return
            if len(comment) > 9999:
                link = self.post.permalink
                submission = self.reddit.subreddit("SnapshillBotEx").submit(
                    "Archives for " + link, selftext=comment[:39999]
                )
                submission.reply(
                    "The original submission can be found " "here:\n\n" + link
                )
                comment = self.post.reply(
                    "Wow, that's a lot of links! The "
                    "snapshots can be [found here.]("
                    + submission.url
                    + ")\n\n"
                    + get_footer()
                )
                log.info("Posted a comment and new submission")
            else:
                comment = self.post.reply(comment)
        except RECOVERABLE_EXC as e:
            log_error(e)
            return
        cur.execute(
            "INSERT INTO links (id, reply) VALUES (?, ?)",
            (self.post.name, comment.name),
        )

    def _build(self):
        parts = [self.header.get(), "Snapshots:"]
        format = "[{name}]({archive})"

        for i, link in enumerate(self.links, 1):
            subparts = []
            log.debug("Found link")

            for archive in link.archives:
                if archive.archived is None:
                    continue

                archive_link = archive.archived

                if not archive_link:
                    log.debug("Not found, using error link")
                    archive_link = (
                        archive.error_link
                        + ' "could not auto-archive; click to resubmit it!"'
                    )
                else:
                    log.debug("Found archive")

                subparts.append(format.format(name=archive.name, archive=archive_link))

            link_text = link.text if self.post.subreddit is not "TheseFuckingAccounts" else link.text.replace('u/', 'u\\/')
            parts.append("{}. {} - {}".format(i, link_text, ", ".join(subparts)))

        parts.append(get_footer())

        return "\n\n".join(parts)


class Header:
    def __init__(self, reddit, settings_wiki, subreddit):
        self.subreddit = subreddit
        self.texts = []
        self._settings = reddit.subreddit(settings_wiki)

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
        try:
            return self._settings.wiki["extxt/" + self.subreddit.lower()].content_md
        except TypeError as err:
            log.debug(
                "could not get wiki content for {} in {} ({})".format(
                    self.subreddit, self._settings, err
                )
            )

        return ""

    def _parse_quotes(self, quotes_str):
        return [q.strip() for q in re.split("(\r)?\n-{3,}(\r)?\n", quotes_str) if q and q.strip()]


class Snapshill:
    def __init__(
        self, username, password, client_id, client_secret, settings_wiki, limit=25
    ):
        self.username = username
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self.limit = limit
        self.settings_wiki = settings_wiki
        self.headers = {}
        self._setup = False
        self.reddit = None

    def run(self):
        """
        Checks through the submissions and archives and posts comments.
        """
        if not self._setup:
            raise Exception("Snapshill not ready yet!")

        submissions = self.reddit.front.new(limit=self.limit)

        for submission in submissions:
            debugTime = time.time()
            warned = False

            log.debug("Found submission.\n" + submission.permalink)

            if not should_notify(submission):
                log.debug("Skipping.")
                continue

            archives = [ArchiveContainer(fix_url(submission.url), submission.title)]

            if submission.is_self and submission.selftext_html is not None:
                log.debug("Found text post...")

                links = BeautifulSoup(unescape(submission.selftext_html)).find_all("a")

                finishedURLs = []

                for anchor in links:
                    if time.time() > debugTime + WARN_TIME and not warned:
                        log.warn(
                            "Spent over {} seconds on post (ID: {})".format(
                                WARN_TIME, submission.name
                            )
                        )

                        warned = True

                    log.debug("Found link in text post...")

                    url = fix_url(anchor["href"])

                    if skip_url(url):
                        continue

                    if url in finishedURLs:
                        continue  # skip for sanity

                    archives.append(ArchiveContainer(url, anchor.contents[0]))
                    finishedURLs.append(url)
                    ratelimit(url)

            Notification(
                self.reddit,
                submission,
                self._get_header(submission.subreddit),
                archives,
            ).notify()
            db.commit()

    def setup(self):
        """
        Logs into reddit and refreshs the header text.
        """
        self._login()
        self.refresh_headers()
        self._setup = True

    def quit(self):
        self.headers = {}
        self._setup = False

    def refresh_headers(self):
        """
        Refreshes the header text for all subreddits.
        """
        self.headers = {"all": Header(self.reddit, self.settings_wiki, "all")}
        for subreddit in self.reddit.user.subreddits():
            name = subreddit.display_name.lower()
            log.debug("get header name: {}".format(name))
            self.headers[name] = Header(self.reddit, self.settings_wiki, name)

    def _login(self):
        self.reddit = praw.Reddit(
            client_id=self.client_id,
            client_secret=self.client_secret,
            username=self.username,
            password=self.password,
            user_agent=USER_AGENT,
        )

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

    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")

    limit = int(os.environ.get("LIMIT", 25))
    wait = int(os.environ.get("WAIT", 5))
    refresh = int(os.environ.get("REFRESH", 1800))

    log.info("Starting...")
    snapshill = Snapshill(
        username,
        password,
        client_id,
        client_secret,
        settings_wiki="SnapshillBot",
        limit=limit,
    )
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
