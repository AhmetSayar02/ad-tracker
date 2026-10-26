const BASE_URL = "http://YOUR_SERVER_IP:8080";
const API_KEY = "YOUR_API_SECRET_TOKEN";
let activeWinId = null;

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "START_SCAN") {
    startScraperWindow(msg.config);
  } 
  else if (msg.action === "STOP_SCAN") {
    chrome.storage.local.set({ liveStatus: "Durduruldu." });
    if (activeWinId) {
      chrome.windows.remove(activeWinId).catch(() => {});
      activeWinId = null;
    }
  } 
  else if (msg.action === "PUSH_BATCH") {
    // Sayfa içinden gelen veriyi arka planda sunucuya gönder
      fetch(`${BASE_URL}/api/ext/batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
      body: JSON.stringify(msg.payload)
    })
    .then(r => r.json())
    .then(d => sendResponse({ ok: true, data: d }))
    .catch(err => sendResponse({ ok: false, error: err.message }));
    return true; // Asenkron yanıt için şart
  } 
  else if (msg.action === "FINISH_SCAN") {
    fetch(`${BASE_URL}/api/ext/finish`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
      body: JSON.stringify(msg.payload)
    })
    .then(() => sendResponse({ ok: true }))
    .catch(err => sendResponse({ ok: false, error: err.message }));
    return true;
  }
});

async function startScraperWindow(cfg) {
  chrome.storage.local.set({ liveStatus: "Sunucuya bağlanılıyor...", liveCount: 0 });

  let existingIds = [];
  try {
    const res = await fetch(`${BASE_URL}/api/ext/init?keyword=${encodeURIComponent(cfg.keyword)}&country=${cfg.country}`, {
      headers: { "X-API-Key": API_KEY }
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const initData = await res.json();
    existingIds = initData.existing_ids || [];
  } catch (e) {
    chrome.storage.local.set({ liveStatus: "Sunucu hatası: " + e.message + " (Sunucuda app.py güncellendi mi?)" });
    return;
  }

  const targetUrl = `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=${cfg.country}&q=${encodeURIComponent(cfg.keyword)}&search_type=keyword_unordered&media_type=all`;

  const win = await chrome.windows.create({
    url: targetUrl,
    type: "popup",
    width: 850,
    height: 650,
    focused: false
  });
  activeWinId = win.id;
  const tabId = win.tabs[0].id;

  chrome.tabs.onUpdated.addListener(function listener(tId, info) {
    if (tId === tabId && info.status === "complete") {
      chrome.tabs.onUpdated.removeListener(listener);
      chrome.scripting.executeScript({
        target: { tabId: tabId },
        func: injectedScraperEngine,
        args: [cfg, existingIds]
      });
    }
  });
}

// --- FACEBOOK SAYFASI İÇİNDE ÇALIŞAN KAZIYICI ---
async function injectedScraperEngine(cfg, existingIdsList) {
  const existingSet = new Set(existingIdsList.map(String));
  const seenIds = new Set();
  const maxLimit = parseInt(cfg.maxAds, 10) || 0;
  const isIncremental = (cfg.scanMode === "incremental") && existingSet.size > 0;
  let consecutiveOld = 0;
  let emptyStreak = 0;
  let reachedEnd = false;

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const setStatus = (txt, cnt) => {
    const obj = { liveStatus: txt };
    if (cnt !== undefined) obj.liveCount = cnt;
    chrome.storage.local.set(obj);
  };
  const sendToBg = (action, payload) => new Promise(resolve => {
    chrome.runtime.sendMessage({ action, payload }, resolve);
  });

  setStatus("Sayfa açıldı, sıralama kontrol ediliyor...", 0);
  await sleep(3000);

  // 'En Yeniler' sıralaması seçiliyse otomatik tıkla
  if (cfg.sortBy === "newest") {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    const sortTargets = [];
    while ((node = walker.nextNode())) {
      if (node.nodeValue && node.nodeValue.trim().includes("Sırala")) {
        sortTargets.push(node.parentElement);
      }
    }
    sortTargets.forEach(el => {
      let c = el;
      for (let i = 0; i < 5; i++) {
        if (!c) break;
        c.click();
        c.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
        c.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
        c = c.parentElement;
      }
    });
    await sleep(1000);

    const w2 = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let n2;
    while ((n2 = w2.nextNode())) {
      const v = (n2.nodeValue || "").trim();
      if (v === "En yeniler" || v === "Most recent") {
        let c = n2.parentElement;
        for (let i = 0; i < 4; i++) {
          if (!c) break;
          c.click();
          c.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
          c.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
          c = c.parentElement;
        }
        break;
      }
    }
    setStatus("Sıralama 'En Yeniler' seçildi, taranıyor...");
    await sleep(2500);
  }

  let step = 0;
  let lastHeight = 0;

  while (step < 2000) {
    step++;
    const idRegex = /(?:Kütüphane Kodu|Kütüphane Kimliği|Library ID)[\s:]*([0-9]{12,20})/i;
    const copyRegex = /([0-9]+)\s+reklam bu kreatifi/i;

    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    const matchedEls = [];

    while ((node = walker.nextNode())) {
      const val = node.nodeValue;
      if (val && (val.includes("Kütüphane") || val.includes("Library ID"))) {
        const p = node.parentElement;
        if (p && !p.closest('[data-scraped="1"]')) matchedEls.push(p);
      }
    }

    const batch = [];
    for (const el of matchedEls) {
      if (el.closest('[data-scraped="1"]')) continue;
      let cardEl = el;
      for (let up = 0; up < 9; up++) {
        if (!cardEl.parentElement) break;
        const pTxt = cardEl.parentElement.textContent || "";
        const m = pTxt.match(/(?:Kütüphane Kodu|Kütüphane Kimliği|Library ID)[\s:]*[0-9]{12,20}/gi);
        if (m && m.length > 1) break;
        cardEl = cardEl.parentElement;
      }

      const segs = [];
      const cw = document.createTreeWalker(cardEl, NodeFilter.SHOW_TEXT, null, false);
      let tn;
      while ((tn = cw.nextNode())) {
        const s = (tn.nodeValue || "").trim();
        if (s.length > 1) segs.push(s);
      }
      const fullText = segs.join("\n");
      const match = fullText.match(idRegex);
      if (!match) continue;

      cardEl.setAttribute("data-scraped", "1");
      const adId = match[1];
      if (seenIds.has(adId)) continue;
      seenIds.add(adId);

      if (existingSet.has(adId)) {
        consecutiveOld++;
      } else {
        consecutiveOld = 0;
      }

      let copiesCount = 1;
      const cm = fullText.match(copyRegex);
      if (cm) copiesCount = parseInt(cm[1], 10) || 1;

      let pageName = "İşletme Sayfası";
      for (let j = 0; j < segs.length; j++) {
        if (segs[j].toLowerCase() === "sponsorlu" || segs[j].toLowerCase() === "sponsored") {
          if (j > 0) pageName = segs[j - 1];
          break;
        }
      }

      let format = "Görsel / Resim";
      if (cardEl.querySelector("video")) format = "Video";
      else if (cardEl.querySelectorAll('img[src*="fbcdn"], img[src*="scontent"]').length > 2) format = "Carousel / Çoklu";

      const imgs = Array.from(cardEl.querySelectorAll('img[src*="fbcdn"], img[src*="scontent"]'));
      const mediaUrl = imgs.length > 0 ? imgs[imgs.length - 1].src : "";

      let ctaText = "Detay Gör";
      let landingUrl = "";
      cardEl.querySelectorAll("a[href]").forEach(a => {
        const href = a.getAttribute("href") || "";
        if (!href.includes("facebook.com/ads/library") && href.startsWith("http")) {
          landingUrl = href;
          const at = (a.textContent || "").trim();
          if (at && at.length > 1 && at.length < 35) ctaText = at;
        }
      });

      batch.push({
        adId, pageName, copiesCount, format, mediaUrl, ctaText, landingUrl, fullText
      });

      if (maxLimit > 0 && seenIds.size >= maxLimit) break;
    }

    // Veriyi background.js üzerinden sunucuya gönder
    if (batch.length > 0) {
      await sendToBg("PUSH_BATCH", {
        keyword: cfg.keyword,
        country: cfg.country,
        ads: batch
      });
    }

    setStatus(`Adım ${step} | Hızlı taranıyor...`, seenIds.size);

    if (maxLimit > 0 && seenIds.size >= maxLimit) {
      setStatus(`Sınıra ulaşıldı (${maxLimit} reklam). Kaydediliyor...`, seenIds.size);
      break;
    }

    if (isIncremental && consecutiveOld >= 40) {
      setStatus(`Eski kayıtlara ulaşıldı, yeni reklamlar alındı!`, seenIds.size);
      break;
    }

    const prevH = document.body.scrollHeight;
    window.scrollTo(0, document.body.scrollHeight);

    for (let w = 0; w < 10; w++) {
      await sleep(120);
      if (document.body.scrollHeight > prevH) break;
    }

    const currH = document.body.scrollHeight;
    if (batch.length === 0 && currH === lastHeight) {
      emptyStreak++;
      window.scrollBy(0, -500);
      await sleep(200);
      window.scrollTo(0, document.body.scrollHeight);
      await sleep(600);
      if (emptyStreak >= 5) {
        reachedEnd = true;
        break;
      }
    } else {
      emptyStreak = 0;
    }
    lastHeight = currH;
  }

  setStatus("Excel, CSV ve Google Sheets güncelleniyor...", seenIds.size);
  await sendToBg("FINISH_SCAN", {
    keyword: cfg.keyword,
    country: cfg.country,
    reached_end: reachedEnd,
    seen_ids: Array.from(seenIds)
  });

  setStatus(`TAMAMLANDI! (${seenIds.size} reklam işlendi)`, seenIds.size);
  await sleep(1500);
  window.close();
}