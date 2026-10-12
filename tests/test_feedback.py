import csv
import io

import pytest

from app import feedback
from app.database import Database

FULL = {q["id"]: q["options"][0] for q in feedback.QUESTIONS}


@pytest.mark.asyncio
async def test_feedback_requires_all_choices_cooldown_and_csv(tmp_path):
    db = Database(str(tmp_path / "f.sqlite"))
    await db.connect()
    try:
        with pytest.raises(ValueError):
            await feedback.submit(db, "1", "a", None, {"score": "满意"})                  # 没选完
        with pytest.raises(ValueError):
            await feedback.submit(db, "1", "a", None, {**FULL, "score": "我自己编的"})     # 不在选项里
        assert "收到" in await feedback.submit(db, "1", "a", None, {**FULL, "bogus": "x"})
        with pytest.raises(ValueError):
            await feedback.submit(db, "1", "a", None, FULL)                                # 10 分钟内重复
        await feedback.submit(db, "2", "b", None, FULL)
        items = await feedback.listing(db)
    finally:
        await db.close()
    assert len(items) == 2 and "bogus" not in items[1]["answers"]
    rows = list(csv.reader(io.StringIO(feedback.to_csv(items).decode("utf-8-sig"))))
    assert rows[1][1] == "1" and FULL["score"] in rows[1]


def test_questions_fit_discord_modal_limits():
    assert 1 <= len(feedback.QUESTIONS) <= 5
    for q in feedback.QUESTIONS:
        assert len(q["label"]) <= 45 and 2 <= len(q["options"]) <= 25
        assert all(len(o) <= 100 for o in q["options"]) and len(set(q["options"])) == len(q["options"])
