// DeskAudit Tab Reporter — Manifest V3 service worker.
// Sends ONLY the active tab's URL + title to the local DeskAudit agent.
// No passwords, no page content, no cookies. Localhost only.
// Runs every 5 seconds and whenever the active tab changes.

const ENDPOINT = "http://127.0.0.1:8765/tab";

async function reportActiveTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url || !/^https?:\/\//.test(tab.url)) return;
    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        browser: "Chrome",
        url: tab.url,
        title: tab.title || ""
      })
    });
  } catch (e) {
    // DeskAudit is not running — fail silently, try again next time.
  }
}

chrome.tabs.onActivated.addListener(reportActiveTab);
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (tab.active && changeInfo.url) reportActiveTab();
});
chrome.alarms.create("deskaudit-poll", { periodInMinutes: 0.0833 }); // ~5s
chrome.alarms.onAlarm.addListener(reportActiveTab);
reportActiveTab();
