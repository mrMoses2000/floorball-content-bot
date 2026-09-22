import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { App } from './App'

vi.mock('./api', () => ({ api: { bootstrap: vi.fn().mockResolvedValue({
  user: { name: 'Тестовый тренер', language: 'ru', telegram_first_name: 'Тест' },
  access: [{ id: 'coach', label: 'Тренер', granted: true }],
  workflows: [{ mode: 'trainer', label: 'Анкета тренера', session_id: null, status: 'not_started', revision: 0, progress: 0, completed_fields: 0, total_fields: 4, can_submit: false, next_question: 'Укажите город', sections: [] }],
}) } }))

test('shows the cabinet and next action', async () => {
  render(<App />)
  expect(await screen.findByRole('heading', { name: /Тестовый тренер/ })).toBeInTheDocument()
  expect(screen.getByText('Анкета тренера')).toBeInTheDocument()
  expect(screen.getByText('Укажите город')).toBeInTheDocument()
})
