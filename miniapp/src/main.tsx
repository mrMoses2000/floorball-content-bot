import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './styles.css'

window.Telegram?.WebApp.ready()
window.Telegram?.WebApp.expand()

createRoot(document.getElementById('root')!).render(
  <StrictMode><App /></StrictMode>,
)
