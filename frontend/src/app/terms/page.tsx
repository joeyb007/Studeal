"use client";

import Link from "next/link";
import styles from "../privacy/page.module.css";

export default function TermsPage() {
  return (
    <main className={styles.main}>
      <nav className={styles.nav}>
        <Link href="/" className={styles.wordmark}>
          <img src="/logo.svg" alt="" className={styles.logoIcon} />
          studeal
        </Link>
      </nav>

      <article className={styles.article}>
        <h1 className={styles.title}>Terms of Service</h1>
        <p className={styles.date}>Last updated: April 2026</p>

        <section className={styles.section}>
          <h2>Acceptance</h2>
          <p>By creating an account or using studeal, you agree to these terms. If you do not agree, do not use the service.</p>
        </section>

        <section className={styles.section}>
          <h2>The service</h2>
          <p>Studeal is a deal discovery platform. We surface deals from third-party sources and do not sell products directly. Prices and availability are determined by the retailer — we do not guarantee accuracy.</p>
        </section>

        <section className={styles.section}>
          <h2>Your account</h2>
          <p>You are responsible for keeping your account credentials secure. You must be at least 13 years old to use studeal. We reserve the right to suspend or terminate accounts that violate these terms.</p>
        </section>

        <section className={styles.section}>
          <h2>Pro subscription</h2>
          <p>Pro subscriptions are billed monthly via Stripe. You may cancel at any time through your account settings. Refunds are not provided for partial billing periods.</p>
        </section>

        <section className={styles.section}>
          <h2>Affiliate links</h2>
          <p>Some links on studeal may be affiliate links. We may earn a commission if you make a purchase through these links at no additional cost to you.</p>
        </section>

        <section className={styles.section}>
          <h2>Limitation of liability</h2>
          <p>Studeal is provided as-is without warranty of any kind. We are not liable for any loss arising from use of the service or reliance on deal information.</p>
        </section>

        <section className={styles.section}>
          <h2>Changes</h2>
          <p>We may update these terms from time to time. Continued use of the service after changes constitutes acceptance.</p>
        </section>

        <section className={styles.section}>
          <h2>Contact</h2>
          <p>Questions? Email <a href="mailto:legal@studeal.site">legal@studeal.site</a>.</p>
        </section>
      </article>
    </main>
  );
}
