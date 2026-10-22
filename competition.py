"""User-provided submission cutoff, independent of the credit plan's expiry."""
import time
from datetime import datetime
import challenge_rules as rules

END_ISO = '2026-10-23T23:50:00+03:30'
END_AT = int(datetime.fromisoformat(END_ISO).timestamp())


def assert_active(now=None):
    now = time.time() if now is None else now
    rules.assert_plan_active(now)
    if now >= END_AT:
        raise ValueError('مهلت مسابقه جمعه ۱ آبان ساعت ۲۳:۵۰ پایان یافته است؛ اجرای سایه متوقف است.')


def snapshot():
    return dict(ends_at=END_AT, ends_iso=END_ISO, ends_fa='جمعه ۱ آبان ۱۴۰۵ ساعت ۲۳:۵۰',
                timezone='Asia/Tehran', remaining_seconds=max(0, END_AT-int(time.time())))
