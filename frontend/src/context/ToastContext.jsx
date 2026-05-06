import { createContext, useContext, useState, useCallback } from 'react'

const ToastContext = createContext(null)

let _nextId = 0

const TYPE_STYLES = {
  success: 'bg-emerald-900 border-emerald-700 text-emerald-300',
  error:   'bg-red-950 border-red-800 text-red-300',
  info:    'bg-surfaceHigh border-border text-slate-300',
}

const TYPE_ICONS = {
  success: '✓',
  error:   '✕',
  info:    'ℹ',
}

function ToastContainer({ toasts, onRemove }) {
  if (toasts.length === 0) return null
  return (
    <div className="fixed top-4 right-4 z-[100] flex flex-col gap-2 pointer-events-none">
      {toasts.map(t => (
        <div
          key={t.id}
          className={`flex items-center gap-2.5 px-3.5 py-2.5 rounded-lg border shadow-lg
                      text-sm font-medium pointer-events-auto cursor-pointer
                      animate-fade-in ${TYPE_STYLES[t.type] || TYPE_STYLES.info}`}
          onClick={() => onRemove(t.id)}
        >
          <span className="text-base leading-none">{TYPE_ICONS[t.type] || TYPE_ICONS.info}</span>
          {t.message}
        </div>
      ))}
    </div>
  )
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const addToast = useCallback((message, type = 'info', duration = 3000) => {
    const id = ++_nextId
    setToasts(prev => [...prev, { id, message, type }])
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), duration)
  }, [])

  const removeToast = useCallback((id) => {
    setToasts(prev => prev.filter(t => t.id !== id))
  }, [])

  return (
    <ToastContext.Provider value={{ addToast }}>
      {children}
      <ToastContainer toasts={toasts} onRemove={removeToast} />
    </ToastContext.Provider>
  )
}

export function useToast() {
  return useContext(ToastContext)
}
