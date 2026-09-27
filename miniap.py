@app.post("/api/spin")
async def api_spin(data: SpinRequest):

    user = require_webapp(
        data.initData
    )

    user_id = int(
        user["id"]
    )

    add_user(
        user_id=user_id,
        username=user.get("username"),
        first_name=user.get("first_name")
    )

    conn = db()

    row = conn.execute(
        "SELECT prize FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:
        conn.close()

        raise HTTPException(
            status_code=404,
            detail="Foydalanuvchi topilmadi"
        )

    # MUHIM:
    # Agar oldindan mukofot saqlangan bo'lsa,
    # qayta ruletka aylantirilmaydi.
    if row["prize"] and int(row["prize"]) > 0:

        saved_prize = int(
            row["prize"]
        )

        conn.close()

        return {
            "ok": True,
            "prize": saved_prize,
            "already": True
        }

    conn.close()

    # MUHIM:
    # Birinchi marta aylantirishdan oldin
    # Telegram orqali obunani tekshiramiz.
    subscription = await get_subscription_status(
        user_id
    )

    if not subscription["all_subscribed"]:

        return {
            "ok": True,
            "stage": "subscription",
            **subscription
        }

    prize = random.choice(
        PRIZES
    )

    conn = db()

    conn.execute(
        "UPDATE users SET prize=? WHERE user_id=?",
        (prize, user_id)
    )

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "prize": prize,
        "already": False
    }
