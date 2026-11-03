"""Year-end holidays must be inside the historical calendar's query bounds."""

from datetime import date

from toss_trading.marketdata import NY, offline_session


def test_december31_and_new_year_holiday_do_not_raise():
    assert offline_session(date(2022, 12, 31)) is None
    assert offline_session(date(2023, 1, 1)) is None
    assert offline_session(date(2023, 1, 2)) is None
    session = offline_session(date(2023, 1, 3))
    assert session.open.astimezone(NY).strftime("%H:%M") == "09:30"
    assert session.close.astimezone(NY).strftime("%H:%M") == "16:00"
