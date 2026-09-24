export type AccessItem = {
  id: string
  label: string
  granted: boolean
  details?: string
}

export type Field = {
  id: string
  label: string
  question: string
  type: string
  requirement: string
  value: unknown
  display_value?: string | null
  filled: boolean
  missing: boolean
  editable: boolean
  options: { value: string; label: string }[]
  max_length?: number
  minimum?: number | null
  maximum?: number | null
}

export type Workflow = {
  mode: string
  label: string
  session_id: string | null
  status: string
  revision: number
  progress: number
  completed_fields: number
  total_fields: number
  can_submit: boolean
  next_question: string | null
  sections: { title: string; fields: Field[] }[]
}

export type Bootstrap = {
  user: { name: string; language: 'ru' | 'kz'; telegram_first_name: string }
  access: AccessItem[]
  workflows: Workflow[]
}

declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        initData: string
        colorScheme: 'light' | 'dark'
        ready(): void
        expand(): void
        close(): void
        HapticFeedback?: { notificationOccurred(type: 'success' | 'error'): void }
      }
    }
  }
}
