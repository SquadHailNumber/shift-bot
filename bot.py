import os, re, sqlite3, logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TOKEN = os.environ['BOT_TOKEN']
TZ = ZoneInfo(os.getenv('BOT_TIMEZONE', 'Europe/Moscow'))
DB = os.getenv('BOT_DB', '/app/data/shifts.sqlite3' if os.path.isdir('/app/data') else 'shifts.sqlite3')
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
logging.basicConfig(level=logging.INFO)

def conn():
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    c=sqlite3.connect(DB, timeout=15)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c

def init():
    with conn() as c:
        c.execute('CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)')
        c.execute('''CREATE TABLE IF NOT EXISTS shifts (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, name TEXT NOT NULL, start TEXT NOT NULL, finish TEXT, answered INTEGER NOT NULL DEFAULT 0, created_by INTEGER NOT NULL)''')
        if OWNER_ID: c.execute('INSERT OR IGNORE INTO admins VALUES (?)',(OWNER_ID,))

def now(): return datetime.now(TZ)
def today(): return now().date().isoformat()
def authorized(uid):
    with conn() as c: return bool(c.execute('SELECT 1 FROM admins WHERE user_id=?',(uid,)).fetchone())

def kb_for(row):
    sid=row['id']; buttons=[InlineKeyboardButton('✅ Ответил' if row['answered'] else '☐ Ответил',callback_data=f'answer:{sid}')]
    buttons.append(InlineKeyboardButton('✏️ Изменить время',callback_data=f'edit:{sid}'))
    buttons.append(InlineKeyboardButton('🔄 Изменить окончание' if row['finish'] else '🏁 Завершить',callback_data=f'finish:{sid}'))
    return InlineKeyboardMarkup([buttons[:1],buttons[1:]])

def row_text(r):
    status='✅ Ответил' if r['answered'] else '☐ Не ответил'
    if r['finish']:
        a=datetime.fromisoformat(r['day']+'T'+r['start']).replace(tzinfo=TZ)
        b=datetime.fromisoformat(r['finish'])
        mins=round((b-a).total_seconds()/60)
        hours,minutes=divmod(max(0,mins),60)
        return f"{r['name']} | {r['start']}–{b.strftime('%H:%M')} | {hours} ч {minutes:02d} мин\n{status} | Завершена"
    return f"{r['name']} | начало {r['start']}\n{status} | В работе"

async def guard(update):
    if not authorized(update.effective_user.id):
        if update.callback_query: await update.callback_query.answer('Нет доступа',show_alert=True)
        else: await update.effective_message.reply_text('Доступ закрыт. Отправь владельцу свой Telegram ID: /myid')
        return False
    return True

async def start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text('Учёт смен запущен. Отправь:\nКириллов к 7:00\nАллагуро к 7:00\n\nКоманды: /today /report /myid /help')

async def myid(update,ctx): await update.message.reply_text(f'Твой Telegram ID: {update.effective_user.id}')

async def help_cmd(update,ctx):
    if not await guard(update):return
    await update.message.reply_text('Добавление: имя к 7:00 (несколько строк).\n/today — смены сегодня\n/list YYYY-MM-DD — список на дату\n/report — отчёт за текущий месяц\n/report YYYY-MM — отчёт за месяц\n/addadmin ID — добавить администратора (только владелец)\n/removeadmin ID — удалить администратора (только владелец)\n/myid — узнать свой ID\nВремя окончания можно ввести как 12:00 или 2026-11-09 01:00 для ночной смены.')

async def addadmin(update,ctx):
    if update.effective_user.id!=OWNER_ID: return await update.message.reply_text('Только владелец может менять администраторов.')
    try: uid=int(ctx.args[0])
    except (IndexError,ValueError): return await update.message.reply_text('Формат: /addadmin 123456789')
    with conn() as c: c.execute('INSERT OR IGNORE INTO admins VALUES (?)',(uid,))
    await update.message.reply_text(f'Администратор {uid} добавлен.')

async def removeadmin(update,ctx):
    if update.effective_user.id!=OWNER_ID: return await update.message.reply_text('Только владелец может менять администраторов.')
    try: uid=int(ctx.args[0])
    except (IndexError,ValueError): return await update.message.reply_text('Формат: /removeadmin 123456789')
    if uid==OWNER_ID: return await update.message.reply_text('Нельзя удалить владельца.')
    with conn() as c: c.execute('DELETE FROM admins WHERE user_id=?',(uid,))
    await update.message.reply_text('Доступ отозван.')

async def show_list(message, day):
    with conn() as c: rows=c.execute('SELECT * FROM shifts WHERE day=? ORDER BY start,id',(day,)).fetchall()
    if not rows:return await message.reply_text(f'На {day} смен нет.')
    await message.reply_text(f'📋 Смены на {day}: {len(rows)}')
    for r in rows: await message.reply_text(row_text(r),reply_markup=kb_for(r))

async def today_cmd(update,ctx):
    if await guard(update): await show_list(update.message,today())

async def list_cmd(update,ctx):
    if not await guard(update):return
    try: day=date.fromisoformat(ctx.args[0]).isoformat()
    except (IndexError,ValueError):return await update.message.reply_text('Формат: /list 2026-11-08')
    await show_list(update.message,day)

PAT=re.compile(r'^\s*(.+?)\s+к\s+([01]?\d|2[0-3]):([0-5]\d)\s*$',re.I)
async def text_handler(update,ctx):
    if not await guard(update):return
    raw=update.message.text.strip()
    pending=ctx.user_data.pop('pending',None)
    if pending:
        action,sid=pending
        with conn() as c: r=c.execute('SELECT * FROM shifts WHERE id=?',(sid,)).fetchone()
        if not r:return await update.message.reply_text('Смена не найдена.')
        try:
            if action=='finish':
                if re.fullmatch(r'\d{1,2}:\d{2}',raw):
                    h,m=map(int,raw.split(':')); assert 0<=h<=23 and 0<=m<=59
                    start=datetime.fromisoformat(r['day']+'T'+r['start']).replace(tzinfo=TZ)
                    end=datetime.combine(now().date(),datetime.min.time(),TZ).replace(hour=h,minute=m)
                    # For past-day records, default to the shift's day (next day if before start).
                    end=datetime.combine(date.fromisoformat(r['day']),datetime.min.time(),TZ).replace(hour=h,minute=m)
                    if end<start:end+=timedelta(days=1)
                else:
                    end=datetime.strptime(raw,'%Y-%m-%d %H:%M').replace(tzinfo=TZ)
                start=datetime.fromisoformat(r['day']+'T'+r['start']).replace(tzinfo=TZ)
                if end<start:raise ValueError('Окончание раньше начала')
                c.execute('UPDATE shifts SET finish=? WHERE id=?',(end.isoformat(),sid))
            else:
                if not re.fullmatch(r'\d{1,2}:\d{2}',raw):raise ValueError()
                h,m=map(int,raw.split(':'));assert 0<=h<=23 and 0<=m<=59
                new=f'{h:02d}:{m:02d}'
                if r['finish'] and datetime.fromisoformat(r['finish'])<datetime.fromisoformat(r['day']+'T'+new).replace(tzinfo=TZ):raise ValueError()
                c.execute('UPDATE shifts SET start=? WHERE id=?',(new,sid))
            r=c.execute('SELECT * FROM shifts WHERE id=?',(sid,)).fetchone()
        except (ValueError,AssertionError):
            ctx.user_data['pending']=pending
            return await update.message.reply_text('Неверное время. Используй 12:00 или для окончания 2026-11-09 01:00.')
        return await update.message.reply_text('Сохранено:\n'+row_text(r),reply_markup=kb_for(r))
    good=[];bad=[]
    for line in raw.splitlines():
        if not line.strip():continue
        m=PAT.match(line)
        if m:good.append((m.group(1).strip(),f'{int(m.group(2)):02d}:{m.group(3)}'))
        else:bad.append(line)
    if not good:return await update.message.reply_text('Пример ввода:\nКириллов к 7:00\nАллагуро к 8:30')
    with conn() as c:
        for name,start in good:c.execute('INSERT INTO shifts(day,name,start,created_by) VALUES (?,?,?,?)',(today(),name,start,update.effective_user.id))
    await update.message.reply_text(f'Добавлено: {len(good)}'+(f'\nНе распознано: {len(bad)} строк(и)' if bad else ''))
    await show_list(update.message,today())

async def callback(update,ctx):
    if not await guard(update):return
    q=update.callback_query
    try:action,sid=q.data.split(':');sid=int(sid)
    except (ValueError,AttributeError):return await q.answer('Неверная команда')
    with conn() as c:
        r=c.execute('SELECT * FROM shifts WHERE id=?',(sid,)).fetchone()
        if not r:return await q.answer('Смена удалена',show_alert=True)
        if action=='answer':
            c.execute('UPDATE shifts SET answered=1-answered WHERE id=?',(sid,))
            r=c.execute('SELECT * FROM shifts WHERE id=?',(sid,)).fetchone()
    if action=='answer':
        await q.answer('Обновлено')
        return await q.edit_message_text(row_text(r),reply_markup=kb_for(r))
    if action in ('finish','edit'):
        ctx.user_data['pending']=(action,sid)
        await q.answer()
        await q.message.reply_text(f"{r['name']}: введи {'время окончания' if action=='finish' else 'новое время начала'} (например 12:00). Для ночной смены можно указать полную дату окончания: 2026-11-09 01:00")

async def report(update,ctx):
    if not await guard(update):return
    month=ctx.args[0] if ctx.args else today()[:7]
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):return await update.message.reply_text('Формат: /report 2026-11')
    with conn() as c:rows=c.execute('SELECT * FROM shifts WHERE day LIKE ? ORDER BY name,day',(month+'%',)).fetchall()
    totals={};incomplete=0
    for r in rows:
        if not r['finish']:incomplete+=1;continue
        a=datetime.fromisoformat(r['day']+'T'+r['start']).replace(tzinfo=TZ)
        b=datetime.fromisoformat(r['finish'])
        totals[r['name']]=totals.get(r['name'],0)+round((b-a).total_seconds()/60)
    lines=[f'📊 Отчёт {month}']
    for name,mins in sorted(totals.items()):lines.append(f'{name}: {mins//60} ч {mins%60:02d} мин')
    lines.append(f'Незавершённых смен: {incomplete}')
    await update.message.reply_text('\n'.join(lines))

def main():
    if not OWNER_ID:raise RuntimeError('Укажи OWNER_ID в настройках окружения')
    init();app=Application.builder().token(TOKEN).build()
    for command,handler in [('start',start),('help',help_cmd),('myid',myid),('today',today_cmd),('list',list_cmd),('report',report),('addadmin',addadmin),('removeadmin',removeadmin)]:app.add_handler(CommandHandler(command,handler))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.run_polling()

if __name__=='__main__':main()
