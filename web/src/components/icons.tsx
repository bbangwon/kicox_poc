// 원 프로젝트 킷과 같은 선 아이콘 (stroke 1.8, currentColor)
import type { ReactNode } from 'react'

function Svg({ size = 16, children }: { size?: number; children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

export const SparkleIcon = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />
    <path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z" />
  </Svg>
)

export const BookIcon = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
    <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
  </Svg>
)

export const SendIcon = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M12 19V5" />
    <path d="M5 12l7-7 7 7" />
  </Svg>
)

export const StopIcon = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" />
  </Svg>
)

export const ChevronIcon = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M6 9l6 6 6-6" />
  </Svg>
)

export const CheckIcon = ({ size = 10 }: { size?: number }) => (
  <Svg size={size}>
    <path d="M5 12l5 5L20 7" />
  </Svg>
)

export const RefreshIcon = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M21 12a9 9 0 1 1-3-6.7L21 8" />
    <path d="M21 3v5h-5" />
  </Svg>
)
