import { useEffect, useMemo, useState } from 'react'
import {
  ArrowLeft, Check, CheckCircle2, ChevronRight, CircleUserRound,
  ClipboardList, Home, LockKeyhole, MessageCircle, Pencil, RefreshCw,
  ShieldCheck, UserRoundCheck, X,
} from 'lucide-react'
import { ApiError, api } from './api'
import type { Bootstrap, Field, Workflow } from './types'

type Page = 'home' | 'data' | 'access'

const copy = {
  ru: {
    service: 'Floorball Kazakhstan', cabinet: 'Личный кабинет', greeting: 'Здравствуйте',
    home: 'Главная', data: 'Мои данные', access: 'Доступ', next: 'Следующий шаг',
    allDone: 'Продолжите в чате для проверки и отправки', continue: 'Продолжить', start: 'Начать',
    chat: 'Продолжить в чате', completed: 'заполнено', granted: 'Доступ подтверждён',
    pending: 'Доступ не назначен', statusTitle: 'Ваш доступ', edit: 'Изменить',
    save: 'Сохранить', clear: 'Очистить', choose: 'Выберите значение', yes: 'Да', no: 'Нет',
    cancel: 'Отмена', close: 'Закрыть', retry: 'Повторить', loading: 'Загружаем кабинет',
    unavailable: 'Кабинет открывается только из Telegram.',
    empty: 'Для вашего доступа пока нет анкет.', required: 'Нужно заполнить', optional: 'Можно дополнить',
    readOnly: 'Изменяется в чате', back: 'Назад', language: 'Язык',
    view: 'Просмотреть', newForm: 'Начать новую анкету', submitted: 'Анкета доступна для просмотра.',
    homeDescription: 'Здесь видно, что уже заполнено и какой шаг будет следующим.',
    dataDescription: 'Анкеты сохраняются и в приложении, и в чате с ботом.',
    accessDescription: 'Права назначаются федерацией и определяют доступные разделы.',
    newsTip: 'Отправьте боту обычный текст, голосовое и фотографии. Он подготовит поля и переводы, а затем спросит о недостающем.',
  },
  kz: {
    service: 'Floorball Kazakhstan', cabinet: 'Жеке кабинет', greeting: 'Сәлеметсіз бе',
    home: 'Басты бет', data: 'Менің деректерім', access: 'Қолжетімділік', next: 'Келесі қадам',
    allDone: 'Тексеру және жіберу үшін чатта жалғастырыңыз', continue: 'Жалғастыру', start: 'Бастау',
    chat: 'Чатта жалғастыру', completed: 'толтырылды', granted: 'Қолжетімділік расталды',
    pending: 'Қолжетімділік тағайындалмаған', statusTitle: 'Сіздің қолжетімділігіңіз', edit: 'Өзгерту',
    save: 'Сақтау', clear: 'Тазарту', choose: 'Мәнді таңдаңыз', yes: 'Иә', no: 'Жоқ',
    cancel: 'Болдырмау', close: 'Жабу', retry: 'Қайталау', loading: 'Кабинет жүктелуде',
    unavailable: 'Кабинет тек Telegram ішінде ашылады.', empty: 'Сіз үшін сауалнамалар әзірге жоқ.',
    required: 'Толтыру керек', optional: 'Толықтыруға болады', readOnly: 'Чатта өзгертіледі',
    back: 'Артқа', language: 'Тіл',
    view: 'Қарау', newForm: 'Жаңа сауалнама бастау', submitted: 'Сауалнаманы қарап шығуға болады.',
    homeDescription: 'Мұнда толтырылған деректер мен келесі қадам көрсетіледі.',
    dataDescription: 'Сауалнамалар қолданбада да, ботпен чатта да сақталады.',
    accessDescription: 'Құқықтарды федерация тағайындайды және олар қолжетімді бөлімдерді анықтайды.',
    newsTip: 'Ботқа мәтін, дауыс хабарламасын және фотосуреттерді жіберіңіз. Ол өрістер мен аудармаларды дайындап, жетіспейтінін сұрайды.',
  },
}

function formatValue(value: unknown, language: 'ru' | 'kz'): string {
  if (value === true) return copy[language].yes
  if (value === false) return copy[language].no
  if (Array.isArray(value)) return `${value.length} ${language === 'kz' ? 'жазба' : 'записей'}`
  if (value && typeof value === 'object') return Object.values(value).filter(Boolean).join(', ')
  return String(value ?? '')
}

function Progress({ value }: { value: number }) {
  return <div className="progress" aria-label={`${value}%`}><span style={{ width: `${value}%` }} /></div>
}

function WorkflowCard({ workflow, onOpen, onStart, language }: {
  workflow: Workflow; onOpen(): void; onStart(): void; language: 'ru' | 'kz'
}) {
  const t = copy[language]
  const active = workflow.session_id !== null
  const writable = workflow.status === 'active'
  return (
    <article className="workflow-card">
      <div className="eyebrow">{active ? `${workflow.progress}% ${t.completed}` : t.next}</div>
      <h3>{workflow.label}</h3>
      {active && <Progress value={workflow.progress} />}
      <p>{active ? (writable ? workflow.next_question || t.allDone : t.submitted) : t.start}</p>
      <button className="text-button" onClick={active ? onOpen : onStart}>
        {active ? (writable ? t.continue : t.view) : t.start}<ChevronRight size={18} aria-hidden="true" />
      </button>
    </article>
  )
}

function Editor({ field, busy, error, language, onClose, onSave, onClear }: {
  field: Field; busy: boolean; error: string; language: 'ru' | 'kz'; onClose(): void;
  onSave(value: unknown): void; onClear(): void
}) {
  const t = copy[language]
  const [value, setValue] = useState(field.value ?? '')
  const inputId = `edit-${field.id}`
  const empty = value === '' || value === null
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (empty || busy) return
    if (field.type === 'integer' || field.type === 'number') {
      const parsed = Number(value)
      if (!Number.isFinite(parsed) || (field.type === 'integer' && !Number.isInteger(parsed))) return
      onSave(parsed)
    } else {
      onSave(value)
    }
  }
  return (
    <div className="sheet-backdrop" role="presentation" onMouseDown={() => { if (!busy) onClose() }}>
      <section className="sheet" role="dialog" aria-modal="true" aria-labelledby="editor-title" onMouseDown={(event) => event.stopPropagation()}>
        <div className="sheet-handle" />
        <div className="sheet-heading">
          <div><div className="eyebrow">{t.edit}</div><h2 id="editor-title">{field.label}</h2></div>
          <button className="icon-button" onClick={onClose} disabled={busy} aria-label={t.close}><X /></button>
        </div>
        <form onSubmit={submit}>
        <label htmlFor={inputId}>{field.question}</label>
        {field.type === 'boolean' ? (
          <select id={inputId} required value={value === '' ? '' : String(value)} onChange={(e) => setValue(e.target.value === '' ? '' : e.target.value === 'true')}>
            <option value="">{t.choose}</option><option value="true">{t.yes}</option><option value="false">{t.no}</option>
          </select>
        ) : field.type === 'choice' ? (
          <select id={inputId} required value={String(value)} onChange={(e) => setValue(e.target.value)}>
            <option value="">{t.choose}</option>{field.options.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        ) : field.type === 'long_text' ? (
          <textarea id={inputId} rows={6} required maxLength={field.max_length} value={String(value)} onChange={(e) => setValue(e.target.value)} autoFocus />
        ) : (
          <input id={inputId} type={field.type === 'integer' || field.type === 'number' ? 'number' : field.type === 'email' ? 'email' : field.type === 'url' ? 'url' : field.type === 'phone' ? 'tel' : 'text'}
            required step={field.type === 'integer' ? 1 : field.type === 'number' ? 'any' : undefined}
            min={field.minimum ?? undefined} max={field.maximum ?? undefined} maxLength={field.max_length}
            value={String(value)} onChange={(e) => setValue(e.target.value)} autoFocus />
        )}
        {error && <p className="sheet-error" role="alert">{error}</p>}
        <div className="sheet-actions">
          <button type="button" className="secondary" disabled={busy} onClick={onClose}>{t.cancel}</button>
          <button type="submit" className="primary" disabled={busy || empty}>{busy ? '…' : t.save}</button>
        </div>
        {field.filled && <button type="button" className="clear-button" disabled={busy} onClick={onClear}>{t.clear}</button>}
        </form>
      </section>
    </div>
  )
}

export function App() {
  const [data, setData] = useState<Bootstrap | null>(null)
  const [page, setPage] = useState<Page>('home')
  const [selectedMode, setSelectedMode] = useState<string | null>(null)
  const [editing, setEditing] = useState<{ workflow: Workflow; field: Field } | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const language = data?.user.language ?? 'ru'
  const t = copy[language]

  const load = async () => {
    setError('')
    try { setData(await api.bootstrap()) } catch (reason) { setError(reason instanceof Error ? reason.message : t.unavailable) }
  }
  useEffect(() => { void load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { window.scrollTo({ top: 0, behavior: 'auto' }) }, [page, selectedMode])

  const selected = useMemo(() => data?.workflows.find((item) => item.mode === selectedMode), [data, selectedMode])
  const primary = data?.workflows.find((item) => item.status === 'active' && !item.can_submit)
    || data?.workflows.find((item) => item.status === 'not_started')
    || data?.workflows.find((item) => item.status === 'active')
    || data?.workflows[0]

  async function action(operation: () => Promise<Bootstrap>): Promise<boolean> {
    setBusy(true); setError('')
    try { setData(await operation()); window.Telegram?.WebApp.HapticFeedback?.notificationOccurred('success'); return true }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось сохранить.')
      if (reason instanceof ApiError && (reason.code === 'stale' || reason.code === 'version_changed')) {
        setEditing(null)
        try { setData(await api.bootstrap()) } catch { /* Keep the actionable original error. */ }
      }
      window.Telegram?.WebApp.HapticFeedback?.notificationOccurred('error')
      return false
    }
    finally { setBusy(false) }
  }

  async function startWorkflow(mode: string) {
    if (busy) return
    if (await action(() => api.start(mode))) setSelectedMode(mode)
  }

  if (!data) return (
    <main className="state-page">
      <div className="brand-mark">KZ</div>
      {error ? <><h1>{t.unavailable}</h1><p>{error}</p><button className="primary" onClick={() => void load()}><RefreshCw size={18} />{t.retry}</button></> : <><div className="loader" /><p>{t.loading}</p></>}
    </main>
  )

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark small">KZ</span><span><b>{t.service}</b><small>{t.cabinet}</small></span></div>
        <button className="language" disabled={busy} onClick={() => void action(() => api.language(language === 'ru' ? 'kz' : 'ru'))} aria-label={t.language}>{language === 'ru' ? 'ҚАЗ' : 'РУС'}</button>
      </header>

      <main className="content">
        {error && <div className="error-banner" role="alert">{error}<button onClick={() => setError('')}><X size={18} /></button></div>}
        {selected ? (
          <section>
            <button className="back-button" onClick={() => setSelectedMode(null)}><ArrowLeft size={18} />{t.back}</button>
            <div className="detail-head"><div className="eyebrow">{selected.progress}% {t.completed}</div><h1>{selected.label}</h1><Progress value={selected.progress} /><p>{selected.status === 'active' ? selected.next_question || t.allDone : t.submitted}</p></div>
            {selected.mode === 'news' && <div className="easy-note"><MessageCircle size={22} /><p>{t.newsTip}</p></div>}
            {selected.sections.map((section) => (
              <section className="data-section" key={section.title}>
                <h2>{section.title}</h2>
                <div className="field-list">{section.fields.map((field) => (
                  <div className="field-row" key={field.id}>
                    <span className={`field-state ${field.filled ? 'done' : ''}`}>{field.filled ? <Check size={15} /> : ''}</span>
                    <div className="field-copy"><b>{field.label}</b><span>{field.filled ? (field.display_value || formatValue(field.value, language)) : !field.editable && selected.status === 'active' ? field.question : field.missing ? t.required : t.optional}</span></div>
                    {field.editable ? <button className="icon-button" onClick={() => { setError(''); setEditing({ workflow: selected, field }) }} aria-label={`${t.edit}: ${field.label}`}><Pencil size={17} /></button> : <LockKeyhole size={16} className="muted-icon" aria-label={t.readOnly} />}
                  </div>
                ))}</div>
              </section>
            ))}
            {selected.status !== 'active' && <button className="primary wide" disabled={busy} onClick={() => void startWorkflow(selected.mode)}>{t.newForm}</button>}
            <button className="chat-button" onClick={() => window.Telegram?.WebApp.close()}><MessageCircle size={20} />{t.chat}</button>
          </section>
        ) : page === 'home' ? (
          <section>
            <div className="welcome"><div className="eyebrow">{t.cabinet}</div><h1>{t.greeting},<br />{data.user.name}</h1><p>{t.homeDescription}</p></div>
            {primary && <section className="focus-card"><span className="focus-icon"><ClipboardList /></span><div className="eyebrow">{t.next}</div><h2>{primary.label}</h2><Progress value={primary.progress} /><p>{primary.session_id && primary.status !== 'active' ? t.submitted : primary.next_question || t.allDone}</p><button className="primary wide" disabled={busy} onClick={() => primary.session_id ? setSelectedMode(primary.mode) : void startWorkflow(primary.mode)}>{primary.session_id ? (primary.status === 'active' ? t.continue : t.view) : t.start}<ChevronRight size={18} /></button></section>}
            <div className="section-title"><h2>{t.statusTitle}</h2><button onClick={() => setPage('access')}>{t.access}</button></div>
            <div className="status-strip">{data.access.filter((item) => item.granted).slice(0, 2).map((item) => <div key={item.id}><UserRoundCheck size={20} /><span><b>{item.label}</b><small>{item.details || t.granted}</small></span></div>)}</div>
          </section>
        ) : page === 'data' ? (
          <section><div className="page-heading"><div className="eyebrow">{t.cabinet}</div><h1>{t.data}</h1><p>{t.dataDescription}</p></div><div className="workflow-grid">{data.workflows.length ? data.workflows.map((workflow) => <WorkflowCard key={workflow.mode} workflow={workflow} language={language} onOpen={() => setSelectedMode(workflow.mode)} onStart={() => void startWorkflow(workflow.mode)} />) : <p>{t.empty}</p>}</div></section>
        ) : (
          <section><div className="page-heading"><div className="eyebrow">{t.cabinet}</div><h1>{t.statusTitle}</h1><p>{t.accessDescription}</p></div><div className="access-list">{data.access.map((item) => <article key={item.id} className={item.granted ? 'granted' : ''}><span>{item.granted ? <ShieldCheck /> : <CircleUserRound />}</span><div><h2>{item.label}</h2><p>{item.granted ? (item.details || t.granted) : t.pending}</p></div>{item.granted && <CheckCircle2 className="access-check" />}</article>)}</div></section>
        )}
      </main>

      {!selected && <nav className="bottom-nav" aria-label="Основная навигация">{([
        ['home', Home, t.home], ['data', ClipboardList, t.data], ['access', ShieldCheck, t.access],
      ] as const).map(([id, Icon, label]) => <button key={id} className={page === id ? 'active' : ''} onClick={() => setPage(id)}><Icon size={21} /><span>{label}</span></button>)}</nav>}
      {editing && <Editor field={editing.field} busy={busy} error={error} language={language} onClose={() => { setEditing(null); setError('') }} onSave={(value) => void action(async () => { const result = await api.field(editing.workflow.session_id!, editing.field.id, editing.workflow.revision, value); setEditing(null); return result })} onClear={() => void action(async () => { const result = await api.field(editing.workflow.session_id!, editing.field.id, editing.workflow.revision, null, true); setEditing(null); return result })} />}
    </div>
  )
}
