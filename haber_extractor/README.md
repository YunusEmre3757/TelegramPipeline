# Bağımsız haber metni çıkarıcı

`news_extractor.py` URL'yi indirir, ana haber başlığını ve metnini çıkarır. Telegram, SQLite, Colab, model veya API anahtarı gerektirmez. Yerel bilgisayarda veya sunucuda çalışır. Özet üretmez; haber metnini çıkarır.

Başka projeye `haber_extractor` klasörünü birlikte kopyalayın. Aşağıdaki komutlar projenin kök klasöründen çalıştırılır.

```shell
python -m pip install "requests>=2.31.0" "trafilatura==2.3.0" "lxml>=5.3.0"
```

## Python'dan kullanma

```python
from haber_extractor import fetch_article

sonuc = fetch_article("https://www.ahaber.com.tr/GERCEK-HABER-ADRESI")
if sonuc.error:
    print("Haber alınamadı:", sonuc.error)
else:
    print(sonuc.title)
    print(sonuc.text)
    print("Son adres:", sonuc.metadata["final_url"])

# JSON'a uygun sözlük; ham HTML dahil değildir.
kayit = sonuc.to_dict()
```

Adresteki örnek yolu gerçek bir haber URL'siyle değiştirin. Aynı fonksiyon diğer haber siteleri için de kullanılır; siteye özel CSS seçicisi veya domain ayarı yoktur. A Haber özelinde canlı doğrulama bu ayırma çalışmasında yapılmadı; her sayfada eksiksiz sonuç garantisi yoktur.

```python
from haber_extractor import fetch_article

url_listesi = ["https://site-a.com/haber-1", "https://site-b.com/haber-2"]
for url in url_listesi:
    sonuc = fetch_article(url)
    kayit = {"url": url, **sonuc.to_dict()}
    # Kendi veri tabanınıza veya dosyanıza yazın.
    print(kayit)
```

Eski üçlü dönüş de desteklenir: `baslik, metin, hata = fetch_article(url)`. İndirilmiş HTML için `extract_html(html_baytlari, url)` kullanılır; bu fonksiyon ağ isteği yapmaz ve yalnızca metin döndürür. Başlık ve çıkarım bilgisi de gerekiyorsa `extract_page(html_baytlari, url)` kullanılır; `title`, `text`, `details` döndürür.

## Metnin nasıl bulunduğu

Haber siteleri arama motorları için sayfaya kendi haberini yazar (schema.org `articleBody`). Bu bildirim her sitede aynı biçimdedir. Çıkarıcı önce buna bakar, bu yüzden siteye özel kural gerekmez.

- `details["method"] == "declared"`: sayfa haber gövdesini bildirmiş; metin, o gövdeyi basan bloklardan okunmuştur. Ara başlıklar, listeler ve tablolar korunur. Bu etiket yalnızca yöntemi söyler, metnin eksiksiz ve katkısız olduğunu söylemez; onu iki sayı söyler:
  - `coverage`: bildirilen gövdenin ne kadarının metinde bulunduğu (1.0 tamamı). Eksikliği ölçer, fazlalığı ölçmez. 0.5'in altındaysa bildirime güvenilmez ve `layout` yoluna dönülür.
  - `undeclared`: metnin ne kadarının bildirimde olmadığı (0.0 hiçbiri). Bildirimde olmayan metin yalnızca şu koşulların hepsiyle alınır: sezgisel okuma da onu seçmiş olmalı, cümle uzunluğunda olmalı, bağlantı dizisi veya başlıkla biten bir gezinti satırı olmamalı ve habere bağlı olmalı (başlık ile gövdenin sonu arasında, ya da bildirilen paragraflarla aynı kapta). Giriş paragrafı ve canlı anlatımların açılışı böyle gelir; aynı yerde duran özet kutusu veya tanıtım cümlesi de gelebilir.
- Bir sayfada başka haberlerin bildirimi de olabilir (ilgili haberler, yan sütun, sonsuz kaydırmadaki sonraki haber). JSON-LD ve microdata için kural aynıdır: sayfanın kendi adresini taşıyan bildirim alınır; adres verilmemişse başlığı sayfanın başlığıyla (önce `og:title` ve `<title>`, sonra ilk `<h1>`) uyuşan alınır. Sayfadaki ilk veya en uzun bildirim olmak yeterli değildir. Tek bir bildirim bile, başlığı sayfanınkiyle uyuşmuyorsa kullanılmaz; karşılaştıracak başlık yoksa güvenilerek alınır. Hangisinin sayfaya ait olduğu anlaşılamıyorsa hiçbiri kullanılmaz ve `layout` yoluna dönülür.
- Sayfanın kendi bildirimi biliniyorsa, başka haberlerin bildirdiği metin her iki yolda da metne alınmaz. Bildirimi olmayan bir komşu metin için böyle bir kanıt yoktur: `declared` yolunda `undeclared` payını yükseltir, `layout` yolunda fark edilmez.
- `coverage: 1.0`, seçilen bildirimin tamamının bulunduğunu söyler; doğru bildirimin seçildiğini ayrıca kanıtlamaz.
- `details["method"] == "layout"`: sayfa gövde bildirmemiş (veya bildirdiği yalnızca özet); metin sayfa düzenine bakan sezgiyle seçilmiştir. Bu metni sayfada doğrulayan bir şey yoktur; menü kalıntısı veya eksik paragraf olabilir. Özellikle haberin sonunda duran kısa satırlar (fiyat listesi gibi) düşebilir.
- `details` ayrıca sayfanın bildirdiklerini taşır: `is_article` (sayfa haber değilse `False`, hiçbir şey söylemiyorsa alan yoktur), `published_at`, `modified_at` (UTC; site saat dilimi vermediyse alan yoktur), `canonical_url`.
- Başlık metnin içinde tekrar edilmez; `title` alanındadır.

## Terminalden kullanma

```shell
python haber_extractor/news_extractor.py "https://site.com/gercek-haber" --out haber.json
```

`--out` verilmezse JSON terminale yazılır. Çıktı alanları `title`, `text`, `error`, `status`, `final_url`, `content_type`, `extraction` (yukarıdaki `details`); `status` değeri `ok`, `empty` veya `error` olur. Hata durumunda süreç çıkış kodu 1'dir. Dosya UTF-8 yazılır.

## Mevcut özellikler ve sınırlar

- URL normalleştirme, kısa bağlantılar ve en fazla 8 yönlendirme.
- Türkçe karakterleri ham HTML baytlarından çözümleme; ana metin, bağlantılar, listeler ve tabloları Markdown biçiminde koruma.
- Sayfa gövde bildirmediğinde Trafilatura iki kez okur: geniş okuma metni belirler, seçici okuma (`favor_precision=True`) yalnızca bağlantı dizilerini ve iki okumanın anlaştığı paragrafların dışında kalan kısa satırları eleyebilir, paragraf eleyemez. Yorumlar kapalı, tablo ve listeler korunur. Sitenin her haberde tekrarladığı tam cümleler (abonelik notu gibi) bu yolda kalabilir.
- Çıkarıcıdaki değişiklik yeni indirmelerde uygulanır; daha önce kaydedilmiş metinler kendiliğinden değişmez.
- Aynı sunucuya istekler arasında varsayılan 2 saniye bekleme; süreç içindeki çağrılar ortak sınırlayıcı kullanır. `fetch_article` birden çok iş parçacığından çağrılabilir: bir sunucu için beklemek başka sunucuya giden isteği bekletmez.
- 10 MiB indirme sınırı, 15 saniye ağ bekleme sınırı, 45 saniye indirme süre bütçesi. DNS ve yanıt başlıkları için mutlak toplam süre garantisi yoktur.
- HTTP/ağ hataları, boş çıkarım ve HTML dışı içerik ayrımı. PDF metni çıkarılmaz. Sosyal medya ve uygulama mağazası adresleri atlanır.
- Üyelik/giriş, engelleme, JavaScript ile yüklenen metin, görsellerdeki yazı ve devam sayfaları için özel destek yoktur. Haber olmayan tanıtım metni de çıkarılabilir (sayfa bunu bildiriyorsa `is_article` `False` olur); `ok` tüm haberin eksiksiz ve doğru olduğunu kanıtlamaz.

`fetch_article(url, timeout=15, total_timeout=45, max_bytes=10485760, delay=2, stop_event=None)` sınırları çağrı bazında değiştirilebilir. Toplu işlerde aynı süreçte fonksiyonu kullanın; her URL için yeni Python süreci açmak ortak domain beklemesini paylaşmaz. Ham yanıt gerekirse `sonuc.metadata.get("raw_content")` ile alınır.

Telegram projesindeki `news_archive/article_fetcher.py` yalnızca kuyruk, veritabanı ve makale sürümlerini yönetir; aynı bağımsız çıkarıcıyı çağırır.
