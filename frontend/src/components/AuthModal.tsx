"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import styles from "./AuthModal.module.css";

interface AuthModalProps {
  isOpen: boolean;
  onClose: () => void;
  defaultTab?: "login" | "signup";
  callbackQuery?: string;
}

export default function AuthModal({ isOpen, onClose, defaultTab = "signup", callbackQuery }: AuthModalProps) {
  const router = useRouter();
  const [tab, setTab] = useState<"login" | "signup">(defaultTab);
  const [direction, setDirection] = useState<"left" | "right">("left");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [closing, setClosing] = useState(false);

  useEffect(() => {
    if (isOpen) setClosing(false);
  }, [isOpen]);

  if (!isOpen && !closing) return null;

  function close() {
    setClosing(true);
    setTimeout(() => {
      setClosing(false);
      onClose();
    }, 220);
  }

  function switchTab(next: "login" | "signup") {
    setDirection(next === "login" ? "left" : "right");
    setTab(next);
    setError(null);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);

    const result = await signIn("credentials", {
      email,
      password,
      register: tab === "signup" ? "true" : "false",
      redirect: false,
    });

    setLoading(false);

    if (result?.error) {
      if (result.error === "CredentialsSignin") {
        setError(tab === "signup" ? "Registration failed. That email may already be in use." : "Incorrect email or password.");
      } else {
        setError(result.error);
      }
      return;
    }

    const dest = callbackQuery ? `/dashboard?q=${encodeURIComponent(callbackQuery)}` : "/dashboard";
    close();
    router.push(dest);
  }

  async function handleGoogle() {
    const dest = callbackQuery ? `/dashboard?q=${encodeURIComponent(callbackQuery)}` : "/dashboard";
    await signIn("google", { callbackUrl: dest });
  }

  function handleBackdropClick(e: React.MouseEvent) {
    if (e.target === e.currentTarget) close();
  }

  return (
    <div className={[styles.backdrop, closing ? styles.backdropOut : ""].join(" ")} onClick={handleBackdropClick}>
      {/* Aurora orbs */}
      <div className={styles.orb1} />
      <div className={styles.orb2} />
      <div className={styles.orb3} />

      <div className={[styles.card, closing ? styles.cardOut : ""].join(" ")}>
        {/* Close */}
        <button className={styles.closeBtn} onClick={close} aria-label="Close">✕</button>

        {/* Logo */}
        <a href="/" className={styles.logo}>
          <img src="/logo.svg" alt="" className={styles.logoIcon} />
          studeal
        </a>

        {/* Tab toggle */}
        <div className={styles.tabs}>
          <button
            className={[styles.tab, tab === "signup" ? styles.tabActive : ""].join(" ")}
            onClick={() => switchTab("signup")}
          >
            Sign up
          </button>
          <button
            className={[styles.tab, tab === "login" ? styles.tabActive : ""].join(" ")}
            onClick={() => switchTab("login")}
          >
            Log in
          </button>
        </div>

        {/* Sliding content */}
        <div className={styles.slideWrap}>
          <div
            key={tab}
            className={[styles.slideContent, direction === "left" ? styles.slideFromRight : styles.slideFromLeft].join(" ")}
          >
            <h2 className={styles.heading}>
              {tab === "signup" ? "Create an account" : "Welcome back"}
            </h2>

            <form className={styles.form} onSubmit={handleSubmit}>
              <div className={styles.field}>
                <input
                  className={styles.input}
                  type="email"
                  placeholder="Email"
                  autoComplete="email"
                  value={email}
                  onChange={e => setEmail(e.target.value)}
                  required
                />
              </div>
              <div className={styles.field}>
                <input
                  className={styles.input}
                  type="password"
                  placeholder="Password"
                  autoComplete={tab === "signup" ? "new-password" : "current-password"}
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  required
                  minLength={tab === "signup" ? 8 : undefined}
                />
              </div>

              {error && <p className={styles.error}>{error}</p>}

              <button className={styles.submit} type="submit" disabled={loading}>
                {loading
                  ? (tab === "signup" ? "Creating account..." : "Logging in...")
                  : (tab === "signup" ? "Create account" : "Log in")}
              </button>
            </form>

            <div className={styles.divider}>
              <span className={styles.dividerText}>or continue with</span>
            </div>

            <div className={styles.oauthRow}>
              <button className={styles.oauthBtn} onClick={handleGoogle} type="button">
                <svg width="18" height="18" viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
                Google
              </button>
            </div>

            <p className={styles.footer}>
              {tab === "signup"
                ? <>Already have an account? <button className={styles.switchLink} onClick={() => switchTab("login")}>Log in</button></>
                : <>No account? <button className={styles.switchLink} onClick={() => switchTab("signup")}>Sign up</button></>
              }
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
