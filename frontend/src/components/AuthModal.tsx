"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import styles from "./AuthModal.module.css";

interface AuthModalProps {
  isOpen: boolean;
  onClose: () => void;
  defaultTab?: "login" | "signup";
}

export default function AuthModal({ isOpen, onClose, defaultTab = "signup" }: AuthModalProps) {
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

    close();
    router.push("/dashboard");
  }

  async function handleGoogle() {
    await signIn("google", { callbackUrl: "/dashboard" });
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
              <button className={styles.oauthBtn} disabled title="Coming soon" type="button">
                <svg width="16" height="18" viewBox="0 0 814 1000"><path fill="currentColor" d="M788.1 340.9c-5.8 4.5-108.2 62.2-108.2 190.5 0 148.4 130.3 200.9 134.2 202.2-.6 3.2-20.7 71.9-68.7 141.9-42.8 61.6-87.5 123.1-155.5 123.1s-85.5-39.5-164-39.5c-76 0-103.7 40.8-165.9 40.8s-105-37.5-155.5-127.4C46.7 790.7 0 663 0 541.8c0-207.5 135.4-317.3 269-317.3 71 0 130.5 46.4 174.5 46.4 42.7 0 109.2-49.9 188.2-49.9 30.8 0 111.2 2.6 166.6 98.3zm-120.2-175.4c-20.7 24.4-55 43.5-87.5 43.5-4.4 0-8.8-.5-13.2-.5 1.3-34.4 16.3-69.4 37.6-94.5 20.7-23.8 58.2-43.5 88.8-44.4 1 4.4 1.5 8.9 1.5 14.5 0 32.6-13.2 67.4-27.2 81.4z"/></svg>
                Apple
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
