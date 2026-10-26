const SERVER_URL = "http://YOUR_SERVER_IP:8080";
const API_KEY = "YOUR_API_SECRET_TOKEN";

const fields = ["keyword", "country", "sortBy", "scanMode", "maxAds"];

chrome.storage.local.get(fields, (data) => {
  fields.forEach(f => {
    if (data[f] !== undefined) document.getElementById(f).value = data[f];
  });
});

function saveConfig() {
  const cfg = { serverUrl: SERVER_URL };
  fields.forEach(f => cfg[f] = document.getElementById(f).value.trim());
  chrome.storage.local.set(cfg);
  return cfg;
}

document.getElementById("startBtn").addEventListener("click", () => {
  const cfg = saveConfig();
  if (!cfg.keyword) {
    alert("Lütfen bir Anahtar Kelime girin!");
    return;
  }
  chrome.runtime.sendMessage({ action: "START_SCAN", config: cfg });
});

document.getElementById("stopBtn").addEventListener("click", () => {
  chrome.runtime.sendMessage({ action: "STOP_SCAN" });
});

document.getElementById("dlExcel").addEventListener("click", () => {
  const cfg = saveConfig();
  if (!cfg.keyword) return alert("Önce kelime yazın!");
  const url = `${SERVER_URL}/api/export?keyword=${encodeURIComponent(cfg.keyword)}&country=${cfg.country}&fmt=xlsx&key=${API_KEY}`;
  chrome.tabs.create({ url });
});

document.getElementById("dlCsv").addEventListener("click", () => {
  const cfg = saveConfig();
  if (!cfg.keyword) return alert("Önce kelime yazın!");
  const url = `${SERVER_URL}/api/export?keyword=${encodeURIComponent(cfg.keyword)}&country=${cfg.country}&fmt=csv&key=${API_KEY}`;
  chrome.tabs.create({ url });
});

setInterval(() => {
  chrome.storage.local.get(["liveStatus", "liveCount"], (d) => {
    if (d.liveStatus) document.getElementById("stText").innerText = d.liveStatus;
    if (d.liveCount !== undefined) document.getElementById("stCount").innerText = d.liveCount;
  });
}, 800);