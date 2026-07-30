/* Studeal service worker — web push only. */

self.addEventListener("push", (event) => {
  if (!event.data) return;
  let payload;
  try {
    payload = event.data.json();
  } catch {
    payload = { title: "Studeal", body: event.data.text(), url: "/" };
  }
  event.waitUntil(
    self.registration.showNotification(payload.title ?? "Studeal", {
      body: payload.body ?? "",
      icon: "/logo.svg",
      badge: "/logo.svg",
      data: { url: payload.url ?? "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url ?? "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const win of wins) {
        if (win.url === url && "focus" in win) return win.focus();
      }
      return clients.openWindow(url);
    })
  );
});
