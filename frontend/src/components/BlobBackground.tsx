"use client";

import styles from "./BlobBackground.module.css";

interface BlobBackgroundProps {
  active?: boolean;
}

export default function BlobBackground({ active = false }: BlobBackgroundProps) {
  return (
    <div className={styles.wrapper} aria-hidden>
      <div className={`${styles.blob} ${active ? styles.blobActive : ""}`} />
      <div className={`${styles.blobSecondary} ${active ? styles.blobActive : ""}`} />
    </div>
  );
}
