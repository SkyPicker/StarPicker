import requests
import logging
import urllib.error
from redis import StrictRedis
from textwrap import dedent
from textblob import TextBlob

from . import config

R = StrictRedis.from_url(config.REDIS_URL)
LOG = logging.getLogger(__name__)


class BaseReview(object):

    type = None

    SLACK_TEMPLATE = dedent(
        """
        New {self.type} by {self.author}:

        >>>{self.text}
    """
    ).strip()

    def __init__(self, review_id, text, rating=None, author=None):
        assert self.type is not None
        self.id = review_id
        self.text = text
        self._rating = rating
        self._author = author
        self.is_new = R.sismember("starpicker:seen_review_ids", self.redis_key) == 0

    @property
    def redis_key(self):
        return "{self.__class__.__name__}:{self.id}".format(self=self)

    @property
    def author(self):
        return self._author

    @property
    def rating(self):
        if self._rating:
            return self._rating
        elif len(self.text) > 3:
            blob = TextBlob(self.text)

            try:
                if blob.detect_language() == "en":
                    return round(min(max(blob.sentiment.polarity, -0.5), 0.5) * 4 + 3)
            except urllib.error.HTTPError:
                LOG.warning("Rating detection failed: HTTPError")
                return None

    def send_to_slack(self):
        color_map = {1: "danger", 2: "warning", 3: "warning", 5: "good"}

        message = self.SLACK_TEMPLATE.format(self=self)
        if config.USE_EMOTICONS:
            message = self.emoticon + " " + message

        body = {
            "username": "starpicker",
            "attachments": [
                {
                    "fallback": message,
                    "pretext": message.split("\n")[0],
                    "text": self.text,
                    "color": color_map.get(self.rating),
                    "title": "{self.type} #{self.id}".format(self=self),
                    "title_link": self.url,
                    "fields": [
                        {"title": "Author", "value": self.author, "short": True},
                        {"title": "Rating", "value": self.rating or "?", "short": True},
                    ],
                }
            ],
        }

        for webhook_url in config.SLACK_WEBHOOK_URLS:
            response = requests.post(webhook_url, json=body, timeout=5)
            response.raise_for_status()

        R.sadd("starpicker:seen_review_ids", self.redis_key)


class TrustpilotReview(BaseReview):

    type = "Trustpilot review"
    emoticon = ":trustpilot:"

    def __init__(self, review):
        super(TrustpilotReview, self).__init__(
            review["id"],
            review["text"],
            review["stars"],
            review["consumer"]["displayName"],
        )

        self.url = "https://www.trustpilot.com/review/{company_id}/{self.id}".format(
            self=self, company_id=review["businessUnit"]["identifyingName"]
        )


class FacebookRatingReview(BaseReview):

    type = "Facebook review"
    emoticon = ":facebook:"

    def __init__(self, rating):
        super(FacebookRatingReview, self).__init__(
            rating["open_graph_story"]["id"],
            rating.get("review_text", ""),
            rating["rating"],
            "_an unknown reviewer_",
        )

    @property
    def url(self):
        return "https://www.facebook.com/{self.id}".format(self=self)


class FacebookCommentReview(BaseReview):

    type = "Facebook comment"
    emoticon = ":facebook:"

    def __init__(self, comment):
        try:
            author = comment["from"]["id"]
        except KeyError:
            author = "_an unknown commenter_"

        super(FacebookCommentReview, self).__init__(
            comment["id"], comment["message"], author=author
        )

        self.url = comment["permalink_url"]


class TweetReview(BaseReview):

    sentiment_map = {":(": 1, "": None, ":)": 5}
    type = "tweet"
    emoticon = ":twitter:"

    def __init__(self, tweet, sentiment=None):
        super(TweetReview, self).__init__(
            tweet["id"], tweet["text"], self.sentiment_map.get(sentiment), tweet["user"]
        )

    @property
    def url(self):
        return "https://www.twitter.com/{self._author[screen_name]}/status/{self.id}".format(
            self=self
        )

    @property
    def author(self):
        return self._author["name"]
