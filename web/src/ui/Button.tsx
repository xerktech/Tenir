import type { ButtonHTMLAttributes } from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
}

export function Button({ variant = "secondary", className = "", ...rest }: ButtonProps): JSX.Element {
  return <button className={`btn btn-${variant} ${className}`.trim()} {...rest} />;
}
