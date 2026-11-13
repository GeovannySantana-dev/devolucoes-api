// Comportamentos das telas: confirmação, "enviando...", prévia de fotos e leitor de código de barras.

// Tema claro/escuro: alterna e lembra a escolha neste aparelho (sem escolha, segue o celular/computador)
document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const root = document.documentElement;
    const isDark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = isDark ? "light" : "dark";
    try { localStorage.setItem("tema", root.dataset.theme); } catch (e) { /* modo privado */ }
  });
});

// Confirmação antes de ações que vão para o marketplace ou apagam algo
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-confirm]");
  if (el && !window.confirm(el.dataset.confirm)) e.preventDefault();
});

// Evita envio duplo e mostra que está processando (uploads de vídeo podem demorar)
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!form.matches("[data-busy]")) return;
  const btn = form.querySelector("button[type=submit]");
  if (btn) {
    btn.disabled = true;
    btn.dataset.label = btn.textContent;
    btn.textContent = form.enctype === "multipart/form-data" ? "Enviando arquivos…" : "Processando…";
  }
});
window.addEventListener("pageshow", () => {
  document.querySelectorAll("button[data-label]").forEach((b) => { b.disabled = false; b.textContent = b.dataset.label; });
});

// Fotos da câmera têm 2-4 MB: reduz para no máx. 1920 px em JPEG (~300 KB) antes de enviar.
// Continua nítido para a conferência e para a contestação, e economiza espaço e dados móveis.
const MAX_SIDE = 1920;
const SKIP_BELOW = 600 * 1024;

async function compressImage(file) {
  if (!/^image\/(jpeg|png|webp)$/.test(file.type) || file.size < SKIP_BELOW || !window.createImageBitmap) return file;
  try {
    const bmp = await createImageBitmap(file); // aplica a rotação da foto (EXIF)
    const scale = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
    bmp.close?.();
    const blob = await new Promise((ok) => canvas.toBlob(ok, "image/jpeg", 0.82));
    if (!blob || blob.size >= file.size) return file;
    const name = file.name.replace(/\.(png|webp|jpe?g)$/i, "") + ".jpg";
    return new File([blob], name, { type: "image/jpeg", lastModified: file.lastModified });
  } catch {
    return file; // se o navegador não conseguir, envia o original
  }
}

// Fotos e vídeos pela câmera: cada "Tirar foto" / "Gravar vídeo" abre a câmera e o arquivo se soma à lista
// (dá para tirar várias fotos seguidas). As fotos, já comprimidas, vão junto no campo "arquivos".
// Os vídeos são grandes: começam a subir assim que são gravados, com a porcentagem na tela, enquanto a
// pessoa escolhe a condição e escreve as observações. O "Confirmar" espera só o que falta.
const UPLOAD_HEADERS = { "X-Background-Upload": "1" };

function uploadInBackground(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const body = new FormData();
    body.append("arquivos", file, file.name || "video.mp4");
    xhr.open("POST", url);
    Object.entries(UPLOAD_HEADERS).forEach(([k, v]) => xhr.setRequestHeader(k, v));
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* resposta não JSON */ }
      if (xhr.status === 200 && data.ids) return resolve(data.ids[0]);
      const err = new Error(data.erro || `o servidor respondeu ${xhr.status}`);
      err.retry = xhr.status >= 500 || xhr.status === 0; // 4xx (ex.: arquivo grande demais) não adianta repetir
      reject(err);
    };
    xhr.onerror = xhr.ontimeout = () => reject(Object.assign(new Error("sem conexão com o servidor"), { retry: true }));
    xhr.send(body);
  });
}

// Wi-Fi da bancada caiu no meio do envio: tenta de novo sozinho (3 s, 10 s, 30 s, depois a cada 60 s) e,
// sem internet, espera ela voltar. O vídeo continua na memória da página até subir.
const RETRY_WAITS = [3, 10, 30, 60];
const RETRY_MAX = 12;

async function uploadWithRetry(url, file, onProgress, onWait) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await uploadInBackground(url, file, onProgress);
    } catch (err) {
      if (!err.retry || attempt + 1 >= RETRY_MAX) throw err;
      onProgress(0);
      if (!navigator.onLine) {
        onWait(null);
        await new Promise((ok) => window.addEventListener("online", ok, { once: true }));
      }
      const secs = RETRY_WAITS[Math.min(attempt, RETRY_WAITS.length - 1)];
      for (let left = secs; left > 0; left--) {
        onWait(left);
        await new Promise((ok) => setTimeout(ok, 1000));
      }
      onWait(undefined);
    }
  }
}

// Câmera dentro da página: "Tirar foto" e "Gravar vídeo" abrem a câmera aqui mesmo, em vez do app Câmera.
// A foto/vídeo fica só na memória e vai direto para o servidor: nada é salvo na galeria do celular.
// O vídeo sai em 720p a ~1 Mbit/s (1 min ≈ 8 MB), sem áudio. Sem suporte, os botões abrem a câmera do celular.
const CAM_VIDEO = { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } };
const CAM_BITRATE = 1_000_000;
const CAM_MAX_SECONDS = 180;
const CAM_TYPES = ["video/mp4;codecs=avc1", "video/mp4", "video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];

function setupInAppCamera(box, addFiles) {
  const dialog = box.querySelector("[data-cam]");
  if (!dialog || !window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return;
  const video = dialog.querySelector("video");
  const info = dialog.querySelector("[data-cam-info]");
  const count = dialog.querySelector("[data-cam-count]");
  const shot = dialog.querySelector("[data-cam-shot]");
  const mimeType = CAM_TYPES.find((t) => MediaRecorder.isTypeSupported(t)) || "";
  let stream, mode, recorder, chunks, timer, photos;
  const stamp = () => new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");

  const stopRecording = () => recorder && recorder.state !== "inactive" && recorder.stop();
  const close = () => {
    stopRecording();
    clearInterval(timer);
    stream?.getTracks().forEach((t) => t.stop());
    stream = null;
    video.srcObject = null;
    if (dialog.open) dialog.close();
  };

  const open = async (which) => {
    mode = which;
    photos = 0;
    dialog.classList.toggle("cam-video", mode === "video");
    dialog.classList.remove("cam-recording");
    shot.setAttribute("aria-label", mode === "video" ? "Começar a gravar" : "Tirar foto");
    info.textContent = mode === "video" ? "Toque no botão vermelho para gravar" : "";
    count.textContent = "";
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: CAM_VIDEO, audio: false });
    } catch (err) {
      alert("Não foi possível abrir a câmera. Permita o acesso à câmera para este site " +
        "(cadeado ao lado do endereço) ou use \"escolher da galeria\".");
      return;
    }
    dialog.showModal();
    video.srcObject = stream;
    await video.play().catch(() => {});
  };

  const takePhoto = () => {
    const w = video.videoWidth, h = video.videoHeight;
    if (!w) return;
    const scale = Math.min(1, MAX_SIDE / Math.max(w, h));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => {
      if (!blob) return;
      addFiles([new File([blob], `foto_${stamp()}.jpg`, { type: "image/jpeg" })]);
      photos += 1;
      count.textContent = `${photos} foto(s)`;
    }, "image/jpeg", 0.82);
    video.animate?.([{ opacity: 0.3 }, { opacity: 1 }], 200); // "piscada" de confirmação
  };

  const startRecording = () => {
    chunks = [];
    recorder = new MediaRecorder(stream, { ...(mimeType && { mimeType }), videoBitsPerSecond: CAM_BITRATE });
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = () => {
      clearInterval(timer);
      dialog.classList.remove("cam-recording");
      const type = (recorder.mimeType || mimeType || "video/webm").split(";")[0];
      const blob = new Blob(chunks, { type });
      chunks = [];
      if (blob.size) addFiles([new File([blob], `video_${stamp()}.${type.includes("mp4") ? "mp4" : "webm"}`, { type })]);
      close();
    };
    recorder.start(1000);
    dialog.classList.add("cam-recording");
    shot.setAttribute("aria-label", "Parar a gravação");
    const started = Date.now();
    const tick = () => {
      const s = Math.floor((Date.now() - started) / 1000);
      const size = chunks.reduce((n, c) => n + c.size, 0) / 1048576;
      info.textContent = `● ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} de ${CAM_MAX_SECONDS / 60}:00` +
        (size >= 0.1 ? ` · ${size.toFixed(1).replace(".", ",")} MB` : "");
      if (s >= CAM_MAX_SECONDS) stopRecording();
    };
    tick();
    timer = setInterval(tick, 500);
  };

  shot.addEventListener("click", () => {
    if (mode === "photo") takePhoto();
    else if (recorder?.state === "recording") stopRecording();
    else startRecording();
  });
  dialog.querySelector("[data-cam-close]").addEventListener("click", () => (recorder?.state === "recording" ? stopRecording() : close()));
  dialog.addEventListener("cancel", (e) => { e.preventDefault(); recorder?.state === "recording" ? stopRecording() : close(); });

  // Os botões "Tirar foto" / "Gravar vídeo" (labels com input capture) passam a abrir a câmera da página
  box.querySelectorAll("label:has(input[capture])").forEach((label) => {
    const input = label.querySelector("input[capture]");
    label.addEventListener("click", (e) => {
      e.preventDefault();
      open(input.accept.startsWith("video") ? "video" : "photo");
    });
  });
}

document.querySelectorAll("[data-capture]").forEach((box) => {
  const form = box.closest("form");
  const target = box.querySelector("[data-files]");
  const adders = [...box.querySelectorAll("[data-add]")];
  const previews = box.querySelector("[data-previews]");
  const status = box.querySelector("[data-capture-status]");
  const submit = form.querySelector("button[type=submit]");
  if (!("DataTransfer" in window)) return; // sem isso, cada botão envia o próprio arquivo (nome "arquivos")
  adders.forEach((a) => a.removeAttribute("name"));
  target.name = "arquivos";
  const uploadUrl = form.action.replace(/\/(conferir|midias)$/, "/midias");
  const sent = document.createElement("input");
  sent.type = "hidden";
  sent.name = "enviados";
  sent.value = "0";
  form.appendChild(sent);
  // cada item: { file, bg (vídeo enviado em segundo plano), progress, id, error, job }
  let list = [];
  const mb = (n) => (n / 1048576).toFixed(1).replace(".", ",");
  const pending = () => list.filter((it) => it.bg && !it.id && !it.error);
  const pct = (p) => `${Math.round((p || 0) * 100)}%`;

  const render = () => {
    const dt = new DataTransfer();
    list.filter((it) => !it.bg).forEach((it) => dt.items.add(it.file));
    target.files = dt.files;
    sent.value = String(list.filter((it) => it.bg && !it.error).length);
    previews.innerHTML = "";
    list.forEach((it, i) => {
      const { file } = it;
      const item = document.createElement("div");
      item.className = "preview-item";
      if (file.type.startsWith("image/")) {
        const img = document.createElement("img");
        img.src = it.url ||= URL.createObjectURL(file);
        img.alt = `Foto ${i + 1}`;
        item.appendChild(img);
      } else {
        const v = document.createElement("div");
        v.className = "vid" + (it.error ? " vid-error" : it.bg && !it.id ? " vid-up" : "");
        const state = it.error ? "falhou: remova e grave de novo"
          : it.id ? "✓ enviado"
          : it.wait === null ? "sem internet: envia quando a conexão voltar"
          : it.wait ? `conexão caiu: tentando de novo em ${it.wait} s`
          : it.bg ? `enviando ${pct(it.progress)}` : "";
        v.textContent = `▶ ${mb(file.size)} MB` + (state ? ` · ${state}` : "");
        v.style.setProperty("--p", pct(it.progress));
        item.appendChild(v);
      }
      const del = document.createElement("button");
      del.type = "button";
      del.className = "preview-del";
      del.setAttribute("aria-label", `Remover ${file.type.startsWith("image/") ? "foto" : "vídeo"} ${i + 1}`);
      del.textContent = "✕";
      del.addEventListener("click", () => remove(it));
      item.appendChild(del);
      previews.appendChild(item);
    });
    const photos = list.filter((it) => it.file.type.startsWith("image/")).length;
    const total = list.reduce((s, it) => s + it.file.size, 0);
    status.textContent = !list.length ? ""
      : `${photos} foto(s) e ${list.length - photos} vídeo(s) · ${mb(total)} MB. ` +
        (pending().length ? "O vídeo vai subindo enquanto você termina a conferência."
          : 'Toque em "Tirar foto" de novo para adicionar mais.');
  };

  const remove = (it) => {
    list = list.filter((x) => x !== it);
    if (it.bg) {
      // se já subiu, apaga do servidor; se ainda está subindo, apaga quando terminar
      const drop = (id) => id && fetch(`${uploadUrl}/${id}/excluir`, { method: "POST", headers: UPLOAD_HEADERS }).catch(() => {});
      if (it.id) drop(it.id); else it.job?.then(drop, () => {});
    }
    render();
  };

  const startUpload = (it) => {
    it.bg = true;
    let last = 0;
    it.job = uploadWithRetry(uploadUrl, it.file, (p) => {
      it.progress = p;
      if (Date.now() - last > 400) { last = Date.now(); render(); }
    }, (wait) => { it.wait = wait; render(); });
    it.job.then((id) => { it.id = id; render(); }, (err) => { it.error = err.message; render(); });
  };

  const addFiles = (files) => {
    files.forEach((file) => {
      const it = { file };
      list.push(it);
      if (file.type.startsWith("video/")) startUpload(it);
    });
    render();
  };

  adders.forEach((input) => input.addEventListener("change", async () => {
    if (!input.files.length) return;
    if (submit) submit.disabled = true;
    status.textContent = "⏳ Otimizando…";
    const files = await Promise.all([...input.files].map(compressImage));
    input.value = ""; // permite tirar outra foto logo em seguida
    if (submit) submit.disabled = false;
    addFiles(files);
  }));

  setupInAppCamera(box, addFiles);

  // No "Confirmar"/"Enviar": se ainda tem vídeo subindo, espera terminar e só então envia o formulário
  const failedAlert = () => alert("Um vídeo não foi enviado. Remova ele (✕) e grave de novo antes de confirmar.");
  form.addEventListener("submit", async (e) => {
    if (list.some((it) => it.bg && it.error)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      failedAlert();
      return;
    }
    const going = pending();
    const onlyBackground = form.action.endsWith("/midias") && list.length && list.every((it) => it.bg);
    if (!going.length && !onlyBackground) return; // segue o envio normal
    e.preventDefault();
    e.stopImmediatePropagation();
    if (submit) {
      submit.disabled = true;
      submit.dataset.label ||= submit.textContent;
    }
    const timer = setInterval(() => {
      const left = pending();
      const done = left.reduce((s, it) => s + (it.progress || 0), 0) / (left.length || 1);
      const waiting = left.some((it) => it.wait !== undefined);
      if (submit) submit.textContent = waiting ? "Sem conexão: o vídeo sobe quando ela voltar…" : `Terminando de enviar o vídeo… ${pct(done)}`;
    }, 300);
    await Promise.allSettled(going.map((it) => it.job));
    clearInterval(timer);
    if (list.some((it) => it.bg && it.error)) {
      if (submit) { submit.disabled = false; submit.textContent = submit.dataset.label; }
      failedAlert();
      return;
    }
    if (onlyBackground) {
      location.href = `${location.pathname}?ok=${encodeURIComponent(`${list.length} arquivo(s) enviado(s)`)}`;
      return;
    }
    if (submit) submit.textContent = "Enviando…";
    HTMLFormElement.prototype.submit.call(form);
  });
});
// "Defeito" e "Item diferente do pedido": a foto é obrigatória (prova da perda / da contestação).
// No defeito também aparece o motivo, obrigatório; em "Outro", a descrição vai nas observações.
document.querySelectorAll("form").forEach((form) => {
  const flags = [...form.querySelectorAll("[data-needs-photo]")];
  if (!flags.length) return;
  const hint = form.querySelector("[data-photo-hint]");
  const reasons = form.querySelector("[data-defect-reasons]");
  const notes = form.querySelector("textarea[name=observacoes]");
  const hasFiles = () => [...form.querySelectorAll("input[type=file]")].some((i) => i.files.length) ||
    Number(form.querySelector("input[name=enviados]")?.value) > 0;
  const condition = () => form.querySelector("input[name=condicao]:checked")?.value;
  const sync = () => {
    const flagged = flags.find((f) => f.checked);
    if (hint) {
      hint.hidden = !flagged;
      if (flagged) hint.textContent = hint.dataset[`hint${flagged.value[0].toUpperCase()}${flagged.value.slice(1)}`] || hint.textContent;
    }
    if (reasons) {
      const isDefect = condition() === "defect";
      reasons.hidden = !isDefect;
      reasons.querySelectorAll("input").forEach((r) => { r.required = isDefect; });
    }
    if (notes) notes.required = condition() === "defect" && form.querySelector("input[name=motivo_defeito]:checked")?.value === "other";
  };
  form.querySelectorAll("input[name=condicao], input[name=motivo_defeito]").forEach((r) => r.addEventListener("change", sync));
  sync();
  form.addEventListener("submit", (e) => {
    const flagged = flags.find((f) => f.checked);
    if (flagged && !hasFiles()) {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (hint) hint.hidden = false;
      alert(flagged.dataset.needsPhoto);
    }
  }, true);
});

// Leitor de código de barras USB/Bluetooth: ele "digita" o código muito rápido e aperta Enter.
// Fora de um campo de texto, o app reconhece essa digitação rápida e busca o código em qualquer tela.
(() => {
  const MAX_GAP_MS = 60; // uma pessoa não digita tão rápido; o leitor sim
  const MIN_LENGTH = 6;
  let buffer = "";
  let last = 0;
  const typingInField = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.altKey || e.metaKey || typingInField(document.activeElement)) return;
    const now = performance.now();
    if (now - last > MAX_GAP_MS) buffer = "";
    last = now;
    if (e.key === "Enter") {
      if (buffer.length >= MIN_LENGTH) {
        e.preventDefault();
        const code = buffer;
        buffer = "";
        // Na tela de não entregue o código vai para o formulário de registro; nas demais, para a busca
        const form = document.querySelector("form[action='/app/nao-entregue']");
        if (form) {
          form.querySelector("[name=codigo]").value = code;
          form.requestSubmit();
        } else {
          location.href = `/app?q=${encodeURIComponent(code)}`;
        }
      }
      buffer = "";
    } else if (e.key.length === 1) {
      buffer += e.key;
    }
  });
})();

// Leitor do código de rastreio pela câmera (exige HTTPS ou localhost). O Chrome do Android lê
// sozinho; no Safari do iPhone, que não tem leitor próprio, carrega um leitor em JavaScript.
(() => {
  const btn = document.querySelector("[data-scan]");
  const dialog = document.querySelector("[data-scanner]");
  const field = document.querySelector("[data-scan-target]");
  if (!btn || !dialog || !field || !window.isSecureContext || !navigator.mediaDevices?.getUserMedia) return;
  btn.hidden = false;
  const FORMATS = ["code_128", "code_39", "ean_13", "qr_code", "itf"];
  const POLYFILL = "https://cdn.jsdelivr.net/npm/barcode-detector@3.2.2/dist/es/ponyfill.js";
  let detectorReady;
  const getDetector = () => detectorReady ||= (async () => {
    if ("BarcodeDetector" in window) {
      const supported = await BarcodeDetector.getSupportedFormats().catch(() => []);
      if (supported.includes("code_128")) return new BarcodeDetector({ formats: FORMATS });
    }
    const { BarcodeDetector: Polyfill } = await import(POLYFILL);
    return new Polyfill({ formats: FORMATS });
  })().catch((err) => { detectorReady = null; throw err; });
  const video = dialog.querySelector("video");
  let stream, running;

  const stop = () => {
    running = false;
    stream?.getTracks().forEach((t) => t.stop());
    if (dialog.open) dialog.close();
  };
  dialog.querySelector("[data-scan-close]").addEventListener("click", stop);
  dialog.addEventListener("cancel", stop);

  btn.addEventListener("click", async () => {
    try {
      const loading = getDetector();
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
      // O iPhone só toca o vídeo com ele visível: abre a janela antes do play()
      dialog.showModal();
      video.srcObject = stream;
      await video.play();
      const detector = await loading;
      running = true;
      const tick = async () => {
        if (!running) return;
        const codes = await detector.detect(video).catch(() => []);
        if (codes.length) {
          field.value = codes[0].rawValue.trim();
          stop();
          field.form.submit();
          return;
        }
        requestAnimationFrame(tick);
      };
      tick();
    } catch (err) {
      stop();
      alert("Não foi possível abrir a câmera: " + err.message);
    }
  });
})();

// Fila ao vivo: a cada 30 s busca a mesma página e troca só a lista (abas, contadores e cartões),
// sem recarregar e sem mexer no que a pessoa está digitando na busca.
(() => {
  const region = document.querySelector("[data-live-region]");
  if (!region) return;
  const status = document.querySelector("[data-live-status] span");
  const EVERY = 30000;
  let at = Date.now();
  const busy = () => document.hidden || document.querySelector("dialog[open]") ||
    (document.activeElement && document.activeElement.matches("input, textarea, select"));
  const label = () => {
    const s = Math.round((Date.now() - at) / 1000);
    if (status) status.textContent = s < 5 ? "Fila atualizada agora" : `Fila atualizada há ${s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`} · atualiza sozinha`;
  };
  async function refresh() {
    if (busy()) return;
    try {
      const resp = await fetch(location.href, { headers: { Accept: "text/html", "X-Live-Refresh": "1" }, cache: "no-store" });
      if (!resp.ok || resp.redirected) return; // sessão expirou: a próxima navegação leva ao login
      const doc = new DOMParser().parseFromString(await resp.text(), "text/html");
      const fresh = doc.querySelector("[data-live-region]");
      if (!fresh || busy()) return;
      if (fresh.innerHTML !== region.innerHTML) region.innerHTML = fresh.innerHTML;
      at = Date.now();
      label();
    } catch { /* sem internet agora: tenta de novo na próxima rodada */ }
  }
  setInterval(refresh, EVERY);
  setInterval(label, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - at > EVERY) refresh(); });
})();

// Menus suspensos (Menu, turno, "Mais"): abrir um fecha os outros; tocar fora fecha
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.menu[open], details.shift[open], details.seg-more[open]").forEach((d) => {
    if (!d.contains(e.target)) d.open = false;
  });
});
