import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { App } from './App'
import { api } from './api'
import type { Bootstrap } from './types'

vi.mock('./api', () => ({ api: { field: vi.fn(), bootstrap: vi.fn().mockResolvedValue({
  user: { name: 'Тестовый тренер', language: 'ru', telegram_first_name: 'Тест' },
  access: [{ id: 'coach', label: 'Тренер', granted: true }],
  workflows: [{ mode: 'trainer', label: 'Анкета тренера', session_id: null, status: 'not_started', revision: 0, progress: 0, completed_fields: 0, total_fields: 4, can_submit: false, next_question: 'Укажите город', sections: [] }],
}) } }))

afterEach(cleanup)

test('shows the cabinet and next action', async () => {
  render(<App />)
  expect(await screen.findByRole('heading', { name: /Тестовый тренер/ })).toBeInTheDocument()
  expect(screen.getByText('Анкета тренера')).toBeInTheDocument()
  expect(screen.getByText('Укажите город')).toBeInTheDocument()
})

test('sends a number as a number and leaves an unset boolean unselected', async () => {
  const data: Bootstrap = {
    user: { name: 'Редактор', language: 'ru', telegram_first_name: 'Редактор' },
    access: [],
    workflows: [{
      mode: 'trainer', label: 'Анкета тренера', session_id: '00000000-0000-0000-0000-000000000001',
      status: 'active', revision: 2, progress: 0, completed_fields: 0, total_fields: 2,
      can_submit: false, next_question: 'Укажите стаж',
      sections: [{ title: 'Опыт', fields: [
        { id: 'years', label: 'Стаж', question: 'Сколько лет?', type: 'integer', requirement: 'optional', value: null, filled: false, missing: false, editable: true, options: [], minimum: 0, maximum: 60 },
        { id: 'active', label: 'Работаете?', question: 'Работаете сейчас?', type: 'boolean', requirement: 'optional', value: null, filled: false, missing: false, editable: true, options: [] },
      ] }],
    }],
  }
  vi.mocked(api.bootstrap).mockResolvedValueOnce(data)
  vi.mocked(api.field).mockResolvedValue(data)
  render(<App />)
  await screen.findByRole('heading', { name: /Редактор/ })
  fireEvent.click(screen.getByRole('button', { name: 'Мои данные' }))
  fireEvent.click(screen.getByRole('button', { name: 'Продолжить' }))
  fireEvent.click(screen.getByRole('button', { name: 'Изменить: Работаете?' }))
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('')
  fireEvent.click(screen.getByRole('button', { name: 'Отмена' }))
  fireEvent.click(screen.getByRole('button', { name: 'Изменить: Стаж' }))
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '12' } })
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }))
  await waitFor(() => expect(api.field).toHaveBeenCalledWith(data.workflows[0].session_id, 'years', 2, 12))
})
