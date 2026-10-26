#  Meta Ad Intelligence & Automated Analytics Pipeline (MVP)

An end-to-end automated scraping, data processing, and export engine designed to track Meta Ad Library campaigns, extract structured marketing intelligence, and synchronize data in real-time with Google Sheets and Excel.

---

##  Proje Özeti

Bu proje; dijital pazarlama ve pazar analizi süreçlerinde karşılaşılan **manuel veri toplama darboğazını** ortadan kaldırmak amacıyla geliştirilmiş uçtan uca bir otomasyon sistemidir. 

Meta Reklam Kütüphanesindeki verileri yerel bir Chrome eklentisi aracılığıyla asenkron olarak toplar; merkezi bir FastAPI sunucusunda yapılandırılmış veriye dönüştürür ve eşzamanlı olarak SQLite, Google E-Tablolar ve analize hazır Excel/CSV dosyalarına aktarır.

Proje mimarisi ve tersine mühendislik süreçleri **Claude Code** ve **Cursor** gibi yapay zekâ kodlama asistanları yönlendirilerek hızlı bir **MVP (Minimum Viable Product)** yaklaşımıyla geliştirilmiştir.

---

##  Sistem Mimarisi (Architecture)

```text
[ Meta Ad Library ]
        │
        ▼ (DOM Scraping & Regex Extraction)
[ Chrome Extension (Manifest v3) ]
        │
        ▼ (Authenticated REST API / JSON Batch)
[ FastAPI Backend (Oracle Cloud VPS) ]
        │
   ┌────┴───────────────────────────┐
   ▼                                ▼
[ SQLite Database (WAL Mode) ]   [ Pandas Data Engine ]
                                    │
                                    ├─► Google Sheets (Webhook API)
                                    ├─► Excel (.xlsx) Export
                                    └─► Claude-Ready CSV Export