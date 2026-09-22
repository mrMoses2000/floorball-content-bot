import type { Bootstrap } from './types'

export const demo: Bootstrap = {
  user: { name: 'Алексей Соколов', language: 'ru', telegram_first_name: 'Алексей' },
  access: [
    { id: 'coach', label: 'Тренер', granted: true },
    { id: 'player', label: 'Игрок', granted: false },
    { id: 'city', label: 'Представитель города', granted: true, details: 'Алматы' },
    { id: 'federation', label: 'Представитель федерации', granted: false },
    { id: 'media', label: 'Редактор новостей', granted: true },
  ],
  workflows: [{
    mode: 'trainer', label: 'Продолжить как тренер', session_id: '00000000-0000-0000-0000-000000000001',
    status: 'active', revision: 3, progress: 54, completed_fields: 7, total_fields: 13,
    can_submit: false, next_question: 'Добавьте расписание тренировок и адрес площадки.',
    sections: [
      { title: 'Ответственный', fields: [{ id: 'respondent', label: 'Контактное лицо', question: 'Кто отвечает за сведения?', type: 'record', requirement: 'required_to_start', value: { name: 'Алексей Соколов' }, filled: true, missing: false, editable: false, options: [] }] },
      { title: 'Город', fields: [{ id: 'city_name', label: 'Город и регион', question: 'В каком городе вы работаете?', type: 'text', requirement: 'required_to_start', value: 'Алматы', filled: true, missing: false, editable: true, options: [] }] },
      { title: 'Расписание', fields: [{ id: 'schedule', label: 'Тренировки', question: 'Укажите расписание тренировок.', type: 'record_list', requirement: 'recommended', value: null, filled: false, missing: true, editable: false, options: [] }] },
    ],
  }, {
    mode: 'news', label: 'Создать новость', session_id: null, status: 'not_started', revision: 0,
    progress: 0, completed_fields: 0, total_fields: 12, can_submit: false,
    next_question: 'Расскажите о событии и добавьте фотографии в чате.', sections: [],
  }],
}
