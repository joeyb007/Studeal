"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import styles from "./page.module.css";

function RequestForm() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);

  const submit = async () => {
    await fetch("/api/auth/account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "reset-request", email }),
    }).catch(() => null);
    setSent(true);      // always — existence is never disclosed
  };

  if (sent) {
    return (
      <>
        <h1 className={styles.title}>Check your email</h1>
        <p className={styles.body}>
          If that address has an account, a reset link is on its way. It works
          for 30 minutes.
        </p>
      </>
    );
  }
  return (
    <>
      <h1 className={styles.title}>Reset your password</h1>
      <p className={styles.body}>We&apos;ll email you a link to choose a new one.</p>
      <input
        className={styles.input} type="email" placeholder="you@example.com"
        value={email} onChange={(e) => setEmail(e.target.value)}
      />
      <button className={styles.button} disabled={!email.includes("@")} onClick={submit}>
        Send reset link
      </button>
    </>
  );
}

function ConfirmForm({ token }: { token: string }) {
  const [password, setPassword] = useState("");
  const [state, setState] = useState<"idle" | "done" | "error">("idle");
  const [detail, setDetail] = useState<string | null>(null);

  const submit = async () => {
    const res = await fetch("/api/auth/account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "reset-confirm", token, new_password: password }),
    }).catch(() => null);
    if (res?.ok) {
      setState("done");
    } else {
      const data = await res?.json().catch(() => null);
      setDetail(data?.detail ?? "That didn't work. The link may have expired.");
      setState("error");
    }
  };

  if (state === "done") {
    return (
      <>
        <h1 className={styles.title}>Password updated</h1>
        <p className={styles.body}>
          You can <Link href="/" className={styles.link}>sign in</Link> with it now.
        </p>
      </>
    );
  }
  return (
    <>
      <h1 className={styles.title}>Choose a new password</h1>
      <input
        className={styles.input} type="password" placeholder="New password (8+ characters)"
        value={password} onChange={(e) => setPassword(e.target.value)}
      />
      <button className={styles.button} disabled={password.length < 8} onClick={submit}>
        Set password
      </button>
      {state === "error" && <p className={styles.error}>{detail}</p>}
    </>
  );
}

function ResetInner() {
  const token = useSearchParams().get("token");
  return (
    <div className={styles.card}>
      {token ? <ConfirmForm token={token} /> : <RequestForm />}
    </div>
  );
}

export default function ResetPasswordPage() {
  return (
    <main className={styles.main}>
      <nav className={styles.nav}>
        <Link href="/" className={styles.wordmark}>
          <img src="/logo.svg" alt="" className={styles.logoIcon} />
          studeal
        </Link>
      </nav>
      <Suspense fallback={null}>
        <ResetInner />
      </Suspense>
    </main>
  );
}
