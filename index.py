import streamlit as st
import asyncio
import pandas as pd
import sqlite3
from telethon import TelegramClient
from datetime import datetime, time
import io
import sys
import plotly.express as px

# 1. إعدادات النظام والصفحة
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

st.set_page_config(page_title="رادار البيانات الذكي", layout="wide", page_icon="logo.png")

# بيانات API (تأكد من صحتها)
API_ID = 32909664 
API_HASH = 'd7b39c48aba73cf75cd8d7f7edbd9b28'

# --- وظائف قاعدة البيانات ---
def init_db():
    with sqlite3.connect('telegram_history.db', check_same_thread=False) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS messages (msg_id INTEGER PRIMARY KEY, user TEXT, date TEXT, content TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
        conn.commit()

def get_last_update_time():
    with sqlite3.connect('telegram_history.db', check_same_thread=False) as conn:
        res = conn.execute("SELECT value FROM metadata WHERE key='last_scrape'").fetchone()
    return res[0] if res else "لم يتم التحديث بعد"

def set_last_update_time():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect('telegram_history.db', check_same_thread=False) as conn:
        conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_scrape', ?)", (now,))
        conn.commit()

init_db()

# --- واجهة المستخدم (القائمة الجانبية) ---
with st.sidebar:
    st.image("Syrian_vertical_logo_.png", use_container_width=True) 

    st.header("⚙️ التحكم والجلب")
    chat_input = st.text_input("معرف القناة", value="", placeholder="@your_channel")
    clean_chat = chat_input.strip().split('/')[-1].replace("@", "")
    
    target_user_input = st.text_input("اسم المستخدم المستهدف (اختياري)", placeholder="@username أو اسمه")
    
    st.subheader("🔍 كلمات البحث")
    keywords_raw = st.text_area("الكلمات (افصل بفاصلة)", value="", placeholder="اتركها فارغة لجلب كل رسائل الشخص")
    KEYWORDS = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    
    st.subheader("📅 النطاق الزمني")
    # تم ضبط التاريخ الافتراضي ليكون اليوم
    start_date = st.date_input("جلب الرسائل بدءاً من تاريخ:", value=datetime.now().date())
    msg_limit = st.number_input("حد الفحص الأقصى", 10, 10000, 500)
    
    if st.button("تحديث البيانات 🔄", type="primary", use_container_width=True):
        async def fetch_messages(chat_username, limit, keys, target_user, dt_threshold):
            client = TelegramClient('session_streamlit', API_ID, API_HASH)
            await client.start()
            results = []
            try:
                entity = await client.get_entity(chat_username)
                # جلب الرسائل من الأحدث إلى الأقدم
                async for message in client.iter_messages(entity, limit=limit):
                    if message.text:
                        # تحويل تاريخ الرسالة لتنسيق قابل للمقارنة (بدون Timezone)
                        m_date = message.date.replace(tzinfo=None)
                        
                        # التوقف إذا وصلنا لرسائل أقدم من التاريخ المختار
                        if m_date < dt_threshold:
                            break
                            
                        sender = await message.get_sender()
                        s_username = getattr(sender, 'username', '') or ''
                        s_first_name = getattr(sender, 'first_name', '') or ''
                        full_identity = f"{s_username} {s_first_name}".lower()
                        
                        user_match = True
                        if target_user:
                            user_match = target_user.lower().replace("@", "") in full_identity
                        
                        keyword_match = any(key in message.text for key in keys) if keys else True
                        
                        if user_match and keyword_match:
                            name = getattr(sender, 'title', None) or getattr(sender, 'username', 'Unknown')
                            results.append((message.id, name, m_date.strftime('%Y-%m-%d %H:%M'), message.text))
            except Exception as e: 
                st.error(f"حدث خطأ أثناء الجلب: {e}")
            finally: 
                await client.disconnect()
            return results

        if not clean_chat:
            st.warning("الرجاء إدخال معرف القناة أولاً.")
        else:
            with st.spinner("جاري التحديث وفحص الرسائل..."):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                # تحويل التاريخ المختار لبداية اليوم (الساعة 00:00:00)
                dt_threshold = datetime.combine(start_date, time.min)
                
                new_data = loop.run_until_complete(fetch_messages(clean_chat, msg_limit, KEYWORDS, target_user_input, dt_threshold))
                
                if new_data:
                    with sqlite3.connect('telegram_history.db') as conn:
                        conn.executemany("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)", new_data)
                        conn.commit()
                    set_last_update_time()
                    st.success(f"✅ تم جلب {len(new_data)} رسالة جديدة تطابق المعايير.")
                else:
                    st.info("لم يتم العثور على رسائل جديدة ضمن النطاق الزمني المحدد.")
                st.rerun()

    if st.button("🗑️ مسح السجل بالكامل", use_container_width=True):
        # 1. الاتصال لتنفيذ الحذف العادي
        with sqlite3.connect('telegram_history.db') as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM metadata")
            conn.commit()
        
        # 2. الاتصال بشكل منفصل لتنفيذ VACUUM لتجنب الخطأ
        conn_vacuum = sqlite3.connect('telegram_history.db')
        conn_vacuum.isolation_level = None  # هذا السطر يحل المشكلة
        conn_vacuum.execute("VACUUM")
        conn_vacuum.close()
        
        st.success("✅ تم مسح السجل وتصفير القاعدة بنجاح!")
        st.rerun()

        with sqlite3.connect('telegram_history.db') as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM metadata WHERE key='last_scrape'")
            conn.commit()
        st.rerun()

# --- قسم الإحصائيات والرسوم البيانية ---
st.title("📊 لوحة الإحصائيات الذكية")

with sqlite3.connect('telegram_history.db') as conn:
    df = pd.read_sql_query("SELECT * FROM messages", conn)

if not df.empty:
    df['date'] = pd.to_datetime(df['date'])
    
    c1, c2, c3 = st.columns(3)
    c1.metric("📬 إجمالي الرسائل المخزنة", len(df))
    c2.metric("👤 المستخدمين النشطين", df['user'].nunique())
    c3.metric("🕒 آخر تحديث ناجح", get_last_update_time())

    st.divider()

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("📈 نشاط القناة حسب الساعة")
        df['hour'] = df['date'].dt.hour
        hourly_counts = df['hour'].value_counts().sort_index().reset_index()
        hourly_counts.columns = ['الساعة', 'عدد الرسائل']
        st.plotly_chart(px.line(hourly_counts, x='الساعة', y='عدد الرسائل', markers=True), use_container_width=True)

    with col_b:
        st.subheader("🏆 أكثر المستخدمين تفاعلاً")
        user_counts = df['user'].value_counts().head(10).reset_index()
        user_counts.columns = ['المستخدم', 'عدد الرسائل']
        st.plotly_chart(px.bar(user_counts, x='عدد الرسائل', y='المستخدم', orientation='h'), use_container_width=True)

    with st.expander("📄 عرض البيانات وتصدير Excel"):
        search_query = st.text_input("🔍 ابحث عن كلمة محددة داخل النتائج المحفوظة:", placeholder="اكتب الكلمة هنا...")
        
        filtered_df = df[df['content'].str.contains(search_query, case=False, na=False)] if search_query else df
        st.dataframe(filtered_df[['user', 'date', 'content']], use_container_width=True)
        
        buffer = io.BytesIO()
        filtered_df.to_excel(buffer, index=False, engine='xlsxwriter')
        st.download_button(
            label="📥 تحميل النتائج كملف Excel",
            data=buffer.getvalue(),
            file_name=f"Telegram_Report_{datetime.now().strftime('%Y%m%d')}.xlsx",
            use_container_width=True
        )
else:
    st.info("لا توجد بيانات حالياً. يرجى إدخال معرف القناة والضغط على 'تحديث البيانات'.")