"""Shared helpers. Everything here stays offline — no Google, no Gemini, no LINE."""

import itertools
import json
from datetime import datetime, timezone

from coach import db

_counter = itertools.count()


def new_user(**fields) -> str:
    """A fresh user id, so tests can't see each other's rows."""
    db.init_db()
    uid = f"Utest{next(_counter):04d}"
    db.create_user(uid, display_name="test")
    if fields:
        db.update_user(uid, **fields)
    return uid


def add_food_log(user_id: str, *, kcal=0, protein=0, carbs=0, fat=0,
                 ml=0, kind="food", name="test item", ts=None) -> int:
    """Insert an insights food_log row the way the app does."""
    content = {
        "type": kind,
        "food_name_local" if kind == "food" else "drink_name_local": name,
        "calories_kcal": kcal, "protein_g": protein,
        "total_carbohydrate_g": carbs, "total_fat_g": fat,
        "synced_to_health": True,
    }
    if ml:
        content["volume_ml"] = ml
    stamp = ts or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO insights (user_id, ts, kind, content, delivered) "
            "VALUES (?, ?, 'food_log', ?, 1)",
            (user_id, stamp, json.dumps(content)),
        )
        return cur.lastrowid


def set_targets(user_id: str, **targets) -> None:
    merged = {"kcal": 3200, "protein_g": 190, "fat_g": 85,
              "carbs_g": 385, "water_ml": 3500}
    merged.update(targets)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO goals (user_id, key, value_json, updated_at) "
            "VALUES (?, 'daily_nutrition_targets', ?, datetime('now')) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value_json = excluded.value_json",
            (user_id, json.dumps(merged)),
        )


def point(name, *, package=None, payload_key="nutritionLog", **payload):
    """A Google Health data point. `package` marks it as another app's."""
    app = {"packageName": package} if package else {"googleWebClientId": "ours"}
    return {
        "name": name,
        "dataSource": {"application": app,
                       "platform": "HEALTH_CONNECT" if package else "GOOGLE_WEB_API"},
        payload_key: payload,
    }
