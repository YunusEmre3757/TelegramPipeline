# Telegram haber arşivi

Proje kökünde `./run-local.ps1 -Mode live` ile çalıştırılır. Aktif veriler `local-pilot/archive.db`, kanal ayarları `local-pilot/channels.txt`, Telegram oturumu `local-pilot/telegram.session` altındadır. İzleme ekranı: `python -m news_archive.dashboard`.

## Makale indirme

`live` modunda mesajlardaki bağlantılar arka planda indirilir:

- Aynı anda en fazla 4 site indirilir; bu sayı `channels.txt` içindeki `parallel-sites` ayarıyla değiştirilir (1–16). Bir siteye aynı anda tek istek gider ve aynı sunucuya istekler arasındaki 2 saniyelik bekleme korunur. Toplam süreyi en çok bağlantısı olan site belirler; sayıyı artırmak onu kısaltmaz.
- Sıra mesaj tarihine göredir: en yeni mesajın bağlantısı önce indirilir. Geçmiş çekilirken gelen canlı haber kuyruğun sonunda beklemez.
- Arşiv, başlangıçta bir kez baştan sona taranır; sonrasında yalnızca yeni eklenen mesaj sürümlerine bakılır.
- Site isteği geri çevirirse (HTTP 403, 429, 503) o sitenin kuyruğu 60 saniye bekletilir; geri çevirme sürdükçe bekleme iki katına çıkar (en fazla 1 saat). Diğer siteler devam eder. Geri çevrilen bağlantı `error` olarak kaydedilir ve `fetch-articles --retry` ile yeniden denenir.
- Sağlık kaydındaki `articles` alanı: `pending` (bekleyen ve indirilmekte olan), `active` (o an indirilen site sayısı), `paused` (bekletilen siteler ve kalan saniye).

`./run-local.ps1 -Mode fetch-articles` aynı işçiyi Telegram'a bağlanmadan çalıştırır: bekleyen bağlantıları, `-Retry` ile daha önce `error` ya da `empty` kalanları indirir, sonra JSON dosyalarını yazar. Kurallar `live` ile aynıdır; tek fark, isteği geri çeviren siteyi beklemeden biter ve o sitenin kalan bağlantılarını denenmemiş olarak bildirir. Canlı toplayıcı çalışırken kullanılırsa iki süreç aynı siteye ayrı ayrı istek atar: 2 saniye kuralı süreç başınadır.

## JSON dosyaları

`local-pilot/sources/` altında her kanalın her yayın yılı ayrı dosyadır: `kanaladi_kanalid_yil.json` (örnek `tgrthabertv_1463054138_2026.json`). Yıl, mesajın UTC yayın zamanına göredir. Metni olmayan kanalın dosyası yoktur. Bir kanalın tamamı için o kanalın dosyaları sırayla okunur:

```python
import glob, json
kayitlar = [kayit for yol in sorted(glob.glob("local-pilot/sources/*_1463054138_*.json"))
            for kayit in json.load(open(yol, encoding="utf-8"))["messages"]]
```

- Bir dosya, o yılın bir mesajı ya da makalesi değiştiğinde yeniden yazılır; diğer yıllara dokunulmaz. Aynı dosya en sık 60 saniyede bir yazılır: uzun süredir yazılmamış dosya değişiklikte hemen, yeni yazılmış dosya 60 saniye dolunca yazılır. Yani canlı haberin makalesi JSON'a en fazla bir dakika geriden gelir. Toplayıcı kapanırken bekleyen dosyalar yazılır.
- Dosyalar birer birer yazılır ve yazımlar toplayıcının zamanının onda birini geçmeyecek biçimde aralanır. En büyük dosya (bir yıllık yoğun kanal) 100 MB'ı aşabilir ve yazımı birkaç saniye sürer.
- Başlangıçta tüm dosyalar bir kez yenilenir. Çalışırken şu iki durum bir sonraki başlangıca, `export-json` ya da `fetch-articles` komutuna kadar eski kalabilir: mesaj düzenlendiği için yeniden indirilen bir bağlantıyı taşıyan başka kanal ya da yıldaki kayıt; yeni sürüm eklemeden yalnızca gözlem zamanı düzelen mesaj.
- Yıllara bölünmeden önce yazılmış `kanaladi_kanalid.json` dosyaları, kanalın yıllık dosyaları yazılırken silinir.

Her dosya bir nesnedir:

```json
{
  "schema_version": "3.2",
  "messages": []
}
```

3.2 ile her makaleye, metnin nasıl bulunduğu ve sayfanın kendisi hakkında bildirdikleri eklendi. Bilinmeyen alan yazılmaz:

- `extraction.method`: `declared` ise metin, sitenin sayfada bildirdiği haber gövdesinden okunmuştur. `extraction.coverage` o gövdenin ne kadarının metinde bulunduğunu (eksiklik), `extraction.undeclared` metnin ne kadarının bildirimde olmadığını (fazlalık) söyler. `declared` tek başına "eksiksiz ve doğru" demek değildir; sıkı bir seçim için iki sayıya birlikte bakılır (örneğin `coverage >= 0.95` ve `undeclared <= 0.2`). `layout` ise site gövde bildirmemiştir; metin sezgiyle seçilmiştir ve doğrulanmamıştır.
- `page_published_at`, `page_modified_at`: sitenin bildirdiği yayın ve son değişiklik zamanı (UTC).
- `modified_after_message: true`: sitenin bildirdiği değişiklik zamanı Telegram mesajından sonra. Yalnızca bir zaman uyarısıdır: metnin gerçekten değiştiğini veya bağlantının başka habere gittiğini kanıtlamaz, küçük bir düzeltme de bunu tetikler.
- `not_article: true`: sayfa kendini haber olarak bildirmiyor (gündem sayfası, tanıtım sayfası).
- `canonical_url`: sitenin bildirdiği asıl adres, yalnızca bağlantıdan farklıysa. Aynı haberi farklı bağlantılarla gösteren kayıtları eşlemek için kullanılır.

Çıkarıcı değişse de kayıtlı metinler kendiliğinden değişmez. `article_fetcher.reextract(db, apply=True)` her makalenin saklı sayfasını güncel çıkarıcıyla yeniden okuyup metni yerinde günceller (önce toplayıcı durdurulmalıdır); bunu çalıştıran hazır bir komut yoktur.

- `messages` içinde mesaj kimliği, yayın/gözlem bilgileri, `first_version_id` ve `message_versions` bulunur. Telegram metni yalnızca mesaj sürümünde tutulur.
- Her mesaj sürümündeki `urls`, kaydın kendi `articles` sözlüğüne normalleştirilmiş URL üzerinden bağlanır. Aynı URL'leri taşıyan `article_ids` alanı yoktur. Henüz indirilmeyen veya başarılı metni bulunmayan bağlantı `urls` içinde kalır.
- Her kaydın `articles` alanında son başarılı makalenin `title`, `text`, `version_id`, `fetched_at` bilgileri doğrudan bulunur. Eski sürümler yalnızca varsa `history` altında saklanır. URL anahtarının aynısı olan `article_id` ve ayrı `current_version_id` alanları yoktur. Teknik indirme alanları `final_url` ve `content_type` DB'de saklanır, haber JSON'una yazılmaz.
- `last_check` yalnızca son kontrolde hata varsa veya kontrol zamanı son başarılı sürümün indirilme zamanından farklıysa yazılır. Son başarılı metin başarısız kontrolde korunur.
- İlk gözlem tarihi ve kaynağı `first_version_id` ile seçilen mesaj sürümünden okunur; kökte bu alanlar tekrar edilmez. `edited_at` yalnızca düzenleme tarihi varsa bulunur.
- Her haber kaydı tek başına okunabilir. Aynı URL birden fazla mesajda geçerse makale ilgili her kayıtta bir kez bulunur; bu format dosya çapında tekilleştirme yapmaz.
- JSON okuyucusu `data['messages']` kullanır. Makale metni `record['articles'][normalize_url(url)]['text']` ile alınır.

Veritabanındaki `messages` ilk gözlenen mesajı, `message_versions` geçmişi, `articles` son başarılı sonucu, `article_versions` makale geçmişini tutmaya devam eder. DB'deki ilk/son metin alanları mevcut toplama ve ekran sorguları için korunur; bu değişiklik DB'nin tüm metin alanlarını tekilleştirmez. JSON'daki uzun metin tekrarları kaldırılmıştır.

Web metni çekilme zamanı, mesaj yayımlandığı anda sayfanın aynı olduğunu kanıtlamaz. `publication_text_uncertain` alanı ilk yayın metninin bilinmediği durumlarda korunur. Windows JSON değiştirme hatasına yönelik retry davranışı eklenmemiştir.

Test: `.venv/Scripts/python.exe -B -m unittest discover -s tests -q`.
