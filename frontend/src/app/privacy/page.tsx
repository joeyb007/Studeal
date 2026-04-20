"use client";

import Link from "next/link";
import styles from "./page.module.css";

export default function PrivacyPage() {
  return (
    <main className={styles.main}>
      <nav className={styles.nav}>
        <Link href="/" className={styles.wordmark}>
          <img src="/logo.svg" alt="" className={styles.logoIcon} />
          studeal
        </Link>
      </nav>

      <article className={styles.article}>
        <h1 className={styles.title}>Privacy Policy</h1>
        <p className={styles.date}>Last updated: April 2026</p>

        <section className={styles.section}>
          <h2>What we collect</h2>
          <p>When you create an account we collect your email address and, if you sign in with Google, your Google account name and profile picture. We do not collect payment card details — payments are handled by Stripe.</p>
        </section>

        <section className={styles.section}>
          <h2>How we use it</h2>
          <p>Your email is used to send you deal alerts and digest emails you have opted into by creating a watchlist. We do not sell, rent, or share your personal information with third parties for marketing purposes.</p>
        </section>

        <section className={styles.section}>
          <h2>Cookies and storage</h2>
          <p>We store a session token in your browser to keep you logged in. No third-party advertising cookies are used.</p>
        </section>

        <section className={styles.section}>
          <h2>Data retention</h2>
          <p>Your account data is retained for as long as your account is active. You may request deletion at any time by emailing us at the address below.</p>
        </section>

        <section className={styles.section}>
          <h2>Third-party services</h2>
          <p>We use Resend to deliver emails, Stripe to process payments, and Google OAuth for sign-in. Each of these services has their own privacy policy.</p>
        </section>

        <section className={styles.section}>
          <h2>Contact</h2>
          <p>Questions about this policy? Email <a href="mailto:privacy@studeal.site">privacy@studeal.site</a>.</p>
        </section>
      </article>
    </main>
  );
}
