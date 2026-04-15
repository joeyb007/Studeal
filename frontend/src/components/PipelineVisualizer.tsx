"use client";

import styles from "./PipelineVisualizer.module.css";

const STAGES = ["Search", "Fetch", "Extract", "Score"];

interface PipelineVisualizerProps {
  activeStage: number; // 0-3, -1 = none
}

export default function PipelineVisualizer({ activeStage }: PipelineVisualizerProps) {
  return (
    <div className={styles.pipeline}>
      {STAGES.map((stage, i) => (
        <div key={stage} className={styles.stageWrapper}>
          <div
            className={[
              styles.node,
              i < activeStage ? styles.done : "",
              i === activeStage ? styles.active : "",
            ].join(" ")}
          >
            <span className={styles.label}>{stage}</span>
            {i === activeStage && <span className={styles.pulse} />}
          </div>
          {i < STAGES.length - 1 && (
            <div className={[styles.connector, i < activeStage ? styles.connectorDone : ""].join(" ")} />
          )}
        </div>
      ))}
    </div>
  );
}
