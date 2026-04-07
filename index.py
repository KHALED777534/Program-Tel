import asyncio
import io
import os
import sqlite3
import sys
from datetime import datetime, time, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import plotly.express as px
import streamlit as st
from telethon import TelegramClient
from streamlit_autorefresh import st_autorefresh


# =========================================================
# إعدادات عامة
# =========================================================
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

st.set_page_config(
    page_title="رادار البيانات الذكي",
    layout="wide",
    page_icon="📊",
)

DB_NAME = "telegram_history.db"
SESSION_NAME = "session_streamlit"


# =========================================================
# تحميل مفاتيح Telegram
# =========================================================
def load_telegram_credentials() -> Tuple[Optional[str], Optional[str]]:
    api_id = None
    api_hash = None

    try:
        if "API_ID" in st.secrets:
            api_id = st.secrets["API_ID"]
        if "API_HASH" in st.secrets:
            api_hash = st.secrets["API_HASH"]
    except Exception:
        pass

    if not api_id:
        api_id = os.getenv("API_ID")

    if not api_hash:
        api_hash = os.getenv("API_HASH")

    return api_id, api_hash


API_ID, API_HASH = load_telegram_credentials()


# =========================================================
# قاعدة البيانات
# =========================================================
def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_NAME, check_same_thread=False)


def init_db() -> None:
    with get_connection() as conn:
        cursor = conn.execute("PRAGMA table_info(messages)")
        columns = [row[1] for row in cursor.fetchall()]

        # معالجة البنية القديمة
        if columns and "chat" not in columns:
            conn.execute("DROP TABLE IF EXISTS messages")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                chat TEXT NOT NULL,
                msg_id INTEGER NOT NULL,
                user TEXT,
                username TEXT,
                date TEXT,
                content TEXT,
                PRIMARY KEY (chat, msg_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.commit()


def get_last_update_time() -> str:
    try:
        with get_connection() as conn:
            result = conn.execute(
                "SELECT value FROM metadata WHERE key = 'last_scrape'"
            ).fetchone()
        return result[0] if result else "لم يتم التحديث بعد"
    except Exception:
        return "خطأ في الاتصال"


def set_last_update_time() -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO metadata (key, value)
            VALUES ('last_scrape', ?)
            """,
            (now_str,),
        )
        conn.commit()


def clear_database() -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM metadata")
        conn.commit()

    vacuum_conn = sqlite3.connect(DB_NAME)
    vacuum_conn.isolation_level = None
    vacuum_conn.execute("VACUUM")
    vacuum_conn.close()


def insert_messages(rows: List[Tuple[str, int, str, str, str, str]]) -> int:
    if not rows:
        return 0

    with get_connection() as conn:
        before_changes = conn.total_changes

        conn.executemany(
            """
            INSERT OR IGNORE INTO messages (chat, msg_id, user, username, date, content)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

        after_changes = conn.total_changes
        return after_changes - before_changes


def load_messages_dataframe() -> pd.DataFrame:
    try:
        with get_connection() as conn:
            df = pd.read_sql_query("SELECT * FROM messages", conn)
        return df
    except Exception:
        return pd.DataFrame()


init_db()


# =========================================================
# أدوات مساعدة
# =========================================================
def parse_comma_separated_values(raw_text: str) -> List[str]:
    return [item.strip() for item in raw_text.split(",") if item.strip()]


def normalize_chat_list(raw_text: str) -> List[str]:
    chats = parse_comma_separated_values(raw_text)
    normalized = []
    for chat in chats:
        clean_chat = chat.strip().split("/")[-1].replace("@", "").strip()
        if clean_chat:
            normalized.append(clean_chat)
    return normalized


def normalize_user_list(raw_text: str) -> List[str]:
    users = parse_comma_separated_values(raw_text)
    return [user.lower().replace("@", "").strip() for user in users if user.strip()]


def validate_credentials() -> bool:
    if not API_ID:
        st.error("API_ID غير موجود. تأكد من ملف .streamlit/secrets.toml")
        return False

    if not API_HASH:
        st.error("API_HASH غير موجود. تأكد من ملف .streamlit/secrets.toml")
        return False

    return True


def export_dataframe_to_excel(dataframe: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        dataframe.to_excel(writer, index=False, sheet_name="Messages")
    return buffer.getvalue()


def is_user_match(
    sender_username: str,
    sender_first_name: str,
    sender_title: str,
    target_users: List[str],
) -> bool:
    if not target_users:
        return True

    full_identity = f"{sender_username} {sender_first_name} {sender_title}".lower()
    return any(target_user in full_identity for target_user in target_users)


# =========================================================
# جلب الرسائل من Telegram
# =========================================================
async def fetch_messages_from_single_chat(
    client: TelegramClient,
    chat_username: str,
    limit: int,
    keywords: List[str],
    target_users: List[str],
    dt_threshold: datetime,
) -> List[Tuple[str, int, str, str, str, str]]:
    results: List[Tuple[str, int, str, str, str, str]] = []
    keywords_lower = [keyword.lower() for keyword in keywords]

    entity = await client.get_entity(chat_username)

    async for message in client.iter_messages(entity, limit=limit):
        if not message.text:
            continue

        message_date = message.date.replace(tzinfo=None)

        # الرسائل من الأحدث إلى الأقدم
        if message_date < dt_threshold:
            break

        sender = None
        try:
            sender = await message.get_sender()
        except Exception:
            sender = None

        sender_username = getattr(sender, "username", "") or ""
        sender_first_name = getattr(sender, "first_name", "") or ""
        sender_title = getattr(sender, "title", "") or ""

        user_match = is_user_match(
            sender_username=sender_username,
            sender_first_name=sender_first_name,
            sender_title=sender_title,
            target_users=target_users,
        )

        text_lower = message.text.lower()
        keyword_match = (
            any(keyword in text_lower for keyword in keywords_lower)
            if keywords_lower
            else True
        )

        if user_match and keyword_match:
            display_name = sender_title or sender_first_name or sender_username or "Unknown"
            username_clean = sender_username if sender_username else ""

            results.append(
                (
                    str(chat_username),
                    int(message.id),
                    display_name,
                    username_clean,
                    message_date.strftime("%Y-%m-%d %H:%M:%S"),
                    message.text,
                )
            )

    return results


async def fetch_messages_from_multiple_chats(
    chats: List[str],
    limit: int,
    keywords: List[str],
    target_users: List[str],
    dt_threshold: datetime,
) -> List[Tuple[str, int, str, str, str, str]]:
    if not API_ID or not API_HASH:
        raise ValueError("API credentials are missing.")

    all_results: List[Tuple[str, int, str, str, str, str]] = []
    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

    try:
        await client.start()

        for chat_username in chats:
            try:
                chat_results = await fetch_messages_from_single_chat(
                    client=client,
                    chat_username=chat_username,
                    limit=limit,
                    keywords=keywords,
                    target_users=target_users,
                    dt_threshold=dt_threshold,
                )
                all_results.extend(chat_results)
            except Exception as exc:
                st.warning(f"تعذر جلب البيانات من {chat_username}: {exc}")

    finally:
        await client.disconnect()

    return all_results


def run_fetch_messages(
    chats: List[str],
    limit: int,
    keywords: List[str],
    target_users: List[str],
    dt_threshold: datetime,
) -> List[Tuple[str, int, str, str, str, str]]:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(
            fetch_messages_from_multiple_chats(
                chats=chats,
                limit=limit,
                keywords=keywords,
                target_users=target_users,
                dt_threshold=dt_threshold,
            )
        )
    finally:
        loop.close()


# =========================================================
# التحديث التلقائي
# =========================================================
def auto_refresh_fetch(
    chats: List[str],
    limit: int,
    keywords: List[str],
    target_users: List[str],
    dt_threshold: datetime,
) -> None:
    if not chats:
        st.info("أدخل قناة أو أكثر لتفعيل المراقبة التلقائية.")
        return

    if not validate_credentials():
        return

    try:
        new_rows = run_fetch_messages(
            chats=chats,
            limit=limit,
            keywords=keywords,
            target_users=target_users,
            dt_threshold=dt_threshold,
        )
        inserted_count = insert_messages(new_rows)

        if new_rows:
            set_last_update_time()

        st.session_state["last_auto_result"] = {
            "matched": len(new_rows),
            "inserted": inserted_count,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:
        st.session_state["last_auto_error"] = str(exc)


# =========================================================
# الحالة الافتراضية
# =========================================================
if "auto_monitor_enabled" not in st.session_state:
    st.session_state["auto_monitor_enabled"] = False

if "last_auto_result" not in st.session_state:
    st.session_state["last_auto_result"] = None

if "last_auto_error" not in st.session_state:
    st.session_state["last_auto_error"] = None


# =========================================================
# الواجهة الجانبية
# =========================================================
with st.sidebar:
    st.header("⚙️ التحكم والمراقبة")

    chats_input = st.text_area(
        "معرفات القنوات/المجموعات",
        value="",
        placeholder="@channel1, @group1, @channel2",
        help="افصل بين القنوات أو المجموعات بفاصلة",
    )
    chats = normalize_chat_list(chats_input)

    target_users_input = st.text_area(
        "أسماء المستخدمين المستهدفين (اختياري)",
        value="",
        placeholder="@user1, @user2, @user3",
        help="اتركه فارغًا لمراقبة جميع المستخدمين",
    )
    target_users = normalize_user_list(target_users_input)

    st.subheader("🔍 كلمات البحث")
    keywords_raw = st.text_area(
        "الكلمات (اختياري)",
        value="",
        placeholder="مثال: عاجل, قرار, توظيف",
    )
    keywords = parse_comma_separated_values(keywords_raw)

    st.subheader("📅 النطاق الزمني")
    default_start_date = datetime.now().date() - timedelta(days=7)
    start_date = st.date_input(
        "جلب الرسائل بدءًا من تاريخ:",
        value=default_start_date,
    )

    msg_limit = st.number_input(
        "حد الفحص الأقصى لكل قناة",
        min_value=10,
        max_value=50000,
        value=1000,
        step=10,
    )

    st.subheader("⏱️ المراقبة التلقائية")
    auto_refresh_seconds = st.number_input(
        "التحديث كل كم ثانية؟",
        min_value=5,
        max_value=3600,
        value=30,
        step=5,
    )

    auto_mode = st.toggle("تفعيل المراقبة التلقائية", value=st.session_state["auto_monitor_enabled"])
    st.session_state["auto_monitor_enabled"] = auto_mode

    st.caption(f"آخر تحديث ناجح: {get_last_update_time()}")

    if st.button("تحديث يدوي الآن 🔄", type="primary", use_container_width=True):
        if not chats:
            st.warning("الرجاء إدخال قناة أو مجموعة واحدة على الأقل.")
        elif not validate_credentials():
            st.stop()
        else:
            with st.spinner("جاري الجلب اليدوي..."):
                try:
                    threshold_datetime = datetime.combine(start_date, time.min)
                    new_rows = run_fetch_messages(
                        chats=chats,
                        limit=int(msg_limit),
                        keywords=keywords,
                        target_users=target_users,
                        dt_threshold=threshold_datetime,
                    )

                    inserted_count = insert_messages(new_rows)

                    if new_rows:
                        set_last_update_time()
                        st.success(
                            f"✅ تم العثور على {len(new_rows)} رسالة مطابقة، "
                            f"وتمت إضافة {inserted_count} رسالة جديدة."
                        )
                    else:
                        st.info("لم يتم العثور على رسائل تطابق الشروط المحددة.")

                    st.rerun()
                except Exception as exc:
                    st.error(f"فشل التحديث: {exc}")

    if st.button("🗑️ مسح السجل بالكامل", use_container_width=True):
        try:
            clear_database()
            st.success("✅ تم مسح قاعدة البيانات بالكامل بنجاح.")
            st.rerun()
        except Exception as exc:
            st.error(f"حدث خطأ أثناء المسح: {exc}")


# =========================================================
# تشغيل التحديث التلقائي
# =========================================================
if st.session_state["auto_monitor_enabled"]:
    st_autorefresh(interval=int(auto_refresh_seconds) * 1000, key="telegram_auto_refresh")
    threshold_datetime = datetime.combine(start_date, time.min)
    auto_refresh_fetch(
        chats=chats,
        limit=int(msg_limit),
        keywords=keywords,
        target_users=target_users,
        dt_threshold=threshold_datetime,
    )


# =========================================================
# الواجهة الرئيسية
# =========================================================
st.title("📊 لوحة الإحصائيات الذكية")

if st.session_state["auto_monitor_enabled"]:
    st.success(f"المراقبة التلقائية مفعلة. يتم التحديث كل {int(auto_refresh_seconds)} ثانية.")

if st.session_state.get("last_auto_result"):
    last_result = st.session_state["last_auto_result"]
    st.info(
        f"آخر فحص تلقائي: {last_result['time']} | "
        f"المطابقات: {last_result['matched']} | "
        f"المضاف الجديد: {last_result['inserted']}"
    )

if st.session_state.get("last_auto_error"):
    st.error(f"خطأ في آخر تحديث تلقائي: {st.session_state['last_auto_error']}")


df = load_messages_dataframe()

if df.empty:
    st.info("قاعدة البيانات فارغة حاليًا. استخدم الإعدادات الجانبية لبدء المراقبة.")
else:
    # حماية لو كانت قاعدة قديمة
    required_columns = {"chat", "msg_id", "user", "username", "date", "content"}
    if not required_columns.issubset(df.columns):
        st.error("بنية قاعدة البيانات قديمة أو غير متوافقة. احذف ملف telegram_history.db ثم أعد تشغيل التطبيق.")
        st.stop()

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).copy()

    if df.empty:
        st.warning("تم العثور على بيانات، لكن تعذر تفسير التواريخ بشكل صحيح.")
        st.stop()

    total_messages = len(df)
    active_users = df["user"].nunique()
    active_chats = df["chat"].nunique()

    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
    metric_col1.metric("📬 إجمالي الرسائل", total_messages)
    metric_col2.metric("👤 المستخدمون النشطون", active_users)
    metric_col3.metric("📡 القنوات/المجموعات", active_chats)
    metric_col4.metric("🕒 آخر تحديث ناجح", get_last_update_time())

    st.divider()

    df["hour"] = df["date"].dt.hour
    df["day"] = df["date"].dt.date

    hourly_counts = df["hour"].value_counts().sort_index().reset_index()
    hourly_counts.columns = ["الساعة", "عدد الرسائل"]

    user_counts = df["user"].value_counts().head(10).reset_index()
    user_counts.columns = ["المستخدم", "عدد الرسائل"]

    daily_counts = df.groupby("day").size().reset_index(name="عدد الرسائل")
    daily_counts.columns = ["اليوم", "عدد الرسائل"]

    chat_counts = df["chat"].value_counts().head(10).reset_index()
    chat_counts.columns = ["القناة", "عدد الرسائل"]

    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        st.subheader("📈 نشاط الرسائل حسب الساعة")
        fig_hourly = px.line(
            hourly_counts,
            x="الساعة",
            y="عدد الرسائل",
            markers=True,
        )
        st.plotly_chart(fig_hourly, use_container_width=True)

    with chart_col2:
        st.subheader("🏆 أكثر المستخدمين نشاطًا")
        fig_users = px.bar(
            user_counts,
            x="عدد الرسائل",
            y="المستخدم",
            orientation="h",
        )
        st.plotly_chart(fig_users, use_container_width=True)

    chart_col3, chart_col4 = st.columns(2)

    with chart_col3:
        st.subheader("📅 النشاط اليومي")
        fig_daily = px.line(
            daily_counts,
            x="اليوم",
            y="عدد الرسائل",
            markers=True,
        )
        st.plotly_chart(fig_daily, use_container_width=True)

    with chart_col4:
        st.subheader("📡 أكثر القنوات/المجموعات نشاطًا")
        fig_chats = px.bar(
            chat_counts,
            x="عدد الرسائل",
            y="القناة",
            orientation="h",
        )
        st.plotly_chart(fig_chats, use_container_width=True)

    with st.expander("📄 عرض البيانات وتصدير النتائج", expanded=True):
        search_query = st.text_input(
            "🔍 ابحث داخل الرسائل المحفوظة",
            placeholder="اكتب كلمة أو جزءًا من النص...",
        )

        selected_chat = st.selectbox(
            "اختر قناة/مجموعة للتصفية",
            options=["الكل"] + sorted(df["chat"].dropna().unique().tolist()),
        )

        selected_user = st.selectbox(
            "اختر مستخدمًا للتصفية",
            options=["الكل"] + sorted(df["user"].dropna().unique().tolist()),
        )

        filtered_df = df.copy()

        if search_query:
            filtered_df = filtered_df[
                filtered_df["content"].str.contains(search_query, case=False, na=False)
            ]

        if selected_chat != "الكل":
            filtered_df = filtered_df[filtered_df["chat"] == selected_chat]

        if selected_user != "الكل":
            filtered_df = filtered_df[filtered_df["user"] == selected_user]

        st.write(f"عدد النتائج الحالية: {len(filtered_df)}")

        display_columns = ["chat", "user", "username", "date", "content"]
        st.dataframe(
            filtered_df[display_columns].sort_values(by="date", ascending=False),
            use_container_width=True,
        )

        excel_data = export_dataframe_to_excel(filtered_df)

        st.download_button(
            label="📥 تحميل النتائج كملف Excel",
            data=excel_data,
            file_name=f"Telegram_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )