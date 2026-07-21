import type { ButtonHTMLAttributes, ReactNode } from "react";

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  children: ReactNode;
}

export function IconButton({ label, className = "", children, ...rest }: IconButtonProps): JSX.Element {
  return (
    <button aria-label={label} className={`icon-btn ${className}`.trim()} {...rest}>
      {children}
    </button>
  );
}
