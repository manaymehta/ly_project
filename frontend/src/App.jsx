import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppProvider } from './context/AppContext'
import { ToastProvider } from './context/ToastContext'
import SessionBar from './components/layout/SessionBar'
import MainLayout from './components/layout/MainLayout'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 5000,
    },
  },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AppProvider>
        <ToastProvider>
          <div className="flex flex-col h-screen overflow-hidden bg-bg">
            <SessionBar />
            <MainLayout />
          </div>
        </ToastProvider>
      </AppProvider>
    </QueryClientProvider>
  )
}
