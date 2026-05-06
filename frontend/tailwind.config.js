/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      keyframes: {
        'fade-in': {
          '0%':   { opacity: '0', transform: 'translateY(-6px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 0.15s ease-out',
      },
      colors: {
        bg:        '#0f1117',
        surface:   '#1a1d27',
        surfaceHigh: '#222536',
        border:    '#2a2d3a',
        muted:     '#64748b',
        factory:   '#e07b54',
        drug:      '#6c9dc6',
        distributor: '#82c091',
        hospital:  '#b39ddb',
        api:       '#f0c040',
        risk: {
          high:    '#ef4444',
          medium:  '#f59e0b',
          low:     '#22c55e',
          none:    '#64748b',
        },
        coverage: {
          full:    '#22c55e',
          partial: '#f59e0b',
          zero:    '#ef4444',
        },
      },
    },
  },
  plugins: [],
}
