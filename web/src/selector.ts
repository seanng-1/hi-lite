// SPDX-License-Identifier: GPL-3.0-or-later
interface Segment {
    id: number;
    start: number;
    end: number;
    duration: number;
    filename: string;
    text: string;
    is_keyword: boolean;
  }
  
  async function main() {
    const app = document.getElementById("app")!;
    app.innerHTML = "<h1>Loading...</h1>";
  
    // Try to load segments
    let segments: Segment[];
    let currentVideoId = "default";
  
    try {
      const response = await fetch("/data/segments_manifest.json");
      if (!response.ok) throw new Error("No segments found");
      const data = await response.json();
      segments = data.segments;
      if (!Array.isArray(segments)) throw new Error("Invalid segments data");
    } catch (err) {
      // No segments available, show upload interface
      showUploadInterface();
      return;
    }
  
    // Inject CSS
    injectStyles();
    
    // Build main UI
    buildMainUI(segments, currentVideoId);
  }
  
  function injectStyles() {
    const style = document.createElement("style");
    style.textContent = `
      * { box-sizing: border-box; margin: 0; padding: 0; }
      body { 
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
        background: #1a1d24;
        color: #e0e0e0;
        overflow: hidden;
      }
      .main-container {
        display: flex;
        height: 100vh;
        background: #1a1d24;
      }
      
      .video-panel {
        flex: 0 0 40%;
        display: flex;
        flex-direction: column;
        background: #0d0f14;
        border-right: 1px solid #2a2d35;
      }
      .video-header {
        padding: 16px 20px;
        border-bottom: 1px solid #2a2d35;
        background: #1e2129;
        display: flex;
        justify-content: space-between;
        align-items: center;
      }
      .video-header h1 {
        font-size: 18px;
        font-weight: 500;
        color: #fff;
      }
      .upload-new-btn {
        padding: 8px 16px;
        background: #2a2d35;
        border: 1px solid #3a3d45;
        border-radius: 6px;
        color: #e0e0e0;
        cursor: pointer;
        font-size: 13px;
      }
      .upload-new-btn:hover {
        background: #3a3d45;
      }
      .video-container {
        flex: 1;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 20px;
      }
      #mainVideo {
        width: 100%;
        max-width: 100%;
        border-radius: 8px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.5);
      }
      
      .segments-panel {
        flex: 1;
        display: flex;
        flex-direction: column;
        overflow: hidden;
      }
      .toolbar {
        padding: 16px 20px;
        background: #1e2129;
        border-bottom: 1px solid #2a2d35;
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        align-items: center;
      }
      .toolbar button, .toolbar label {
        padding: 8px 16px;
        background: #2a2d35;
        border: 1px solid #3a3d45;
        border-radius: 6px;
        color: #e0e0e0;
        cursor: pointer;
        font-size: 13px;
        transition: all 0.2s;
      }
      .toolbar button:hover {
        background: #3a3d45;
        border-color: #4a4d55;
      }
      .toolbar button.primary {
        background: #5b7cff;
        border-color: #5b7cff;
        color: white;
        font-weight: 500;
      }
      .toolbar button.primary:hover {
        background: #4a6bef;
      }
      .toolbar button:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .toolbar input[type="checkbox"] {
        margin-right: 6px;
      }
      .stats {
        display: flex;
        gap: 20px;
        padding: 12px 20px;
        background: #1a1d24;
        border-bottom: 1px solid #2a2d35;
        font-size: 13px;
      }
      .stat-item {
        color: #888;
      }
      .stat-item strong {
        color: #e0e0e0;
        margin-left: 4px;
      }
      
      .segments-list {
        flex: 1;
        overflow-y: auto;
        padding: 0;
      }
      .segments-list::-webkit-scrollbar {
        width: 8px;
      }
      .segments-list::-webkit-scrollbar-track {
        background: #1a1d24;
      }
      .segments-list::-webkit-scrollbar-thumb {
        background: #3a3d45;
        border-radius: 4px;
      }
      
      .segment-row {
        display: grid;
        grid-template-columns: 50px 100px 100px 1fr 120px auto;
        gap: 12px;
        padding: 16px 20px;
        border-bottom: 1px solid #2a2d35;
        align-items: center;
        transition: background 0.2s;
        cursor: pointer;
      }
      .segment-row:hover {
        background: #1e2129;
      }
      .segment-row.selected {
        background: #1e2533;
        border-left: 3px solid #5b7cff;
        padding-left: 17px;
      }
      .segment-row.keyword {
        border-left: 3px solid #ff6b6b;
        padding-left: 17px;
      }
      .segment-row.selected.keyword {
        border-left: 3px solid #5b7cff;
      }
      .segment-row.active {
        background: #252933 !important;
      }
      
      .segment-id {
        color: #666;
        font-size: 13px;
        text-align: center;
      }
      .segment-time {
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 6px 12px;
        background: #2a2d35;
        border-radius: 6px;
        font-size: 12px;
        font-family: 'Courier New', monospace;
        color: #aaa;
      }
      .segment-text {
        color: #e0e0e0;
        font-size: 14px;
        line-height: 1.5;
      }
      .segment-info {
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 4px;
        font-size: 12px;
        color: #666;
      }
      .segment-score {
        color: #ff6b6b;
        font-weight: 600;
      }
      .segment-actions {
        display: flex;
        gap: 6px;
      }
      .segment-checkbox {
        width: 18px;
        height: 18px;
        cursor: pointer;
        accent-color: #5b7cff;
      }
      .icon-btn {
        width: 32px;
        height: 32px;
        border: none;
        background: #2a2d35;
        color: #888;
        border-radius: 6px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all 0.2s;
        font-size: 16px;
      }
      .icon-btn:hover {
        background: #3a3d45;
        color: #e0e0e0;
      }
      
      .advanced-section {
        padding: 16px 20px;
        background: #1e2129;
        border-bottom: 1px solid #2a2d35;
        display: none;
      }
      .advanced-section.visible {
        display: block;
      }
      .slider-control {
        margin: 12px 0;
      }
      .slider-control label {
        display: flex;
        justify-content: space-between;
        margin-bottom: 8px;
        font-size: 13px;
        color: #aaa;
      }
      .slider-control input[type="range"] {
        width: 100%;
        height: 6px;
        background: #2a2d35;
        border-radius: 3px;
        outline: none;
      }
      .slider-control input[type="range"]::-webkit-slider-thumb {
        width: 16px;
        height: 16px;
        background: #5b7cff;
        border-radius: 50%;
        cursor: pointer;
      }
      .score-badge {
        padding: 4px 10px;
        background: #5b7cff;
        color: white;
        border-radius: 4px;
        font-weight: 600;
      }
      .preview-info {
        margin-top: 12px;
        padding: 12px;
        background: #2a2d35;
        border-radius: 6px;
        font-size: 13px;
        display: none;
      }
      .preview-info.visible {
        display: block;
      }
      
      .download-link {
        display: none;
        margin: 12px 20px;
        padding: 12px 16px;
        background: #28a745;
        color: white;
        text-decoration: none;
        border-radius: 6px;
        text-align: center;
        font-size: 14px;
        font-weight: 500;
      }
      .download-link.visible {
        display: block;
      }
      .download-link:hover {
        background: #218838;
      }
      .download-row {
        display: none;
        gap: 10px;
        margin: 12px 20px;
        align-items: stretch;
      }
      .download-row.visible {
        display: flex;
      }
      .download-row .download-link {
        display: block;
        flex: 1;
        margin: 0;
      }
      .discard-btn {
        padding: 12px 16px;
        background: #2a2d35;
        border: 1px solid #dc3545;
        color: #dc3545;
        border-radius: 6px;
        cursor: pointer;
        font-size: 14px;
        font-weight: 500;
      }
      .discard-btn:hover {
        background: #dc3545;
        color: white;
      }

      /* Upload interface */
      .upload-container {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        height: 100vh;
        background: #1a1d24;
        padding: 40px;
      }
      .upload-box {
        background: #1e2129;
        border: 2px dashed #3a3d45;
        border-radius: 12px;
        padding: 60px;
        text-align: center;
        max-width: 600px;
        width: 100%;
      }
      .upload-box h1 {
        color: #fff;
        margin-bottom: 20px;
        font-size: 28px;
      }
      .upload-box p {
        color: #aaa;
        margin-bottom: 30px;
        font-size: 16px;
      }
      .upload-input {
        display: none;
      }
      .upload-button {
        padding: 16px 32px;
        background: #5b7cff;
        color: white;
        border: none;
        border-radius: 8px;
        font-size: 16px;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.2s;
      }
      .upload-button:hover {
        background: #4a6bef;
      }
      .upload-button:disabled {
        background: #3a3d45;
        cursor: not-allowed;
        opacity: 0.5;
      }
      .progress-bar {
        width: 100%;
        height: 8px;
        background: #2a2d35;
        border-radius: 4px;
        margin-top: 20px;
        overflow: hidden;
        display: none;
      }
      .progress-bar.visible {
        display: block;
      }
      .progress-fill {
        height: 100%;
        background: #5b7cff;
        transition: width 0.3s;
        width: 0%;
      }
      .status-message {
        margin-top: 20px;
        color: #aaa;
        font-size: 14px;
        display: none;
      }
      .status-message.visible {
        display: block;
      }
    `;
    document.head.appendChild(style);
  }
  
  function showUploadInterface() {
    const app = document.getElementById("app")!;
    
    app.innerHTML = `
      <div class="upload-container">
        <div class="upload-box">
          <h1>🎬 Video Segment Selector</h1>
          <p>Upload a video file to get started</p>
          <input type="file" id="videoInput" class="upload-input" accept="video/*">
          <button id="uploadButton" class="upload-button">Choose Video File</button>
          <div class="progress-bar" id="progressBar">
            <div class="progress-fill" id="progressFill"></div>
          </div>
          <div class="status-message" id="statusMessage">Uploading...</div>
        </div>
      </div>
    `;
  
    const videoInput = document.getElementById("videoInput") as HTMLInputElement;
    const uploadButton = document.getElementById("uploadButton") as HTMLButtonElement;
    const progressBar = document.getElementById("progressBar")!;
    const progressFill = document.getElementById("progressFill") as HTMLDivElement;
    const statusMessage = document.getElementById("statusMessage")!;
  
    uploadButton.addEventListener("click", () => videoInput.click());
  
    videoInput.addEventListener("change", async (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
  
      uploadButton.disabled = true;
      uploadButton.textContent = "Uploading...";
      progressBar.classList.add("visible");
      statusMessage.classList.add("visible");
      statusMessage.textContent = "Uploading video...";
  
      const formData = new FormData();
      formData.append("video", file);
  
      try {
        const response = await fetch("/api/upload", {
          method: "POST",
          body: formData
        });
  
        const result = await response.json();
  
        if (result.status === "ok") {
          const taskId = result.task_id;
          statusMessage.textContent = "Processing video...";
          
          const pollInterval = setInterval(async () => {
            const statusResponse = await fetch(`/api/processing-status/${taskId}`);
            const status = await statusResponse.json();
  
            if (status.progress) {
              progressFill.style.width = `${status.progress}%`;
            }
            
            statusMessage.textContent = status.message || "Processing...";
  
            if (status.status === "complete") {
              clearInterval(pollInterval);
              statusMessage.textContent = "Complete! Loading segments...";
              progressFill.style.width = "100%";
              
              setTimeout(() => location.reload(), 1000);
            } else if (status.status === "error") {
              clearInterval(pollInterval);
              statusMessage.textContent = `Error: ${status.message}`;
              statusMessage.style.color = "#ff6b6b";
              uploadButton.disabled = false;
              uploadButton.textContent = "Try Again";
            }
          }, 2000);
        } else {
          throw new Error(result.message);
        }
      } catch (err) {
        console.error(err);
        statusMessage.textContent = `Upload failed: ${err}`;
        statusMessage.style.color = "#ff6b6b";
        uploadButton.disabled = false;
        uploadButton.textContent = "Choose Video File";
      }
    });
  }
  
  function buildMainUI(segments: Segment[], videoId: string) {
    const app = document.getElementById("app")!;
    
    app.innerHTML = `
      <div class="main-container">
        <div class="video-panel">
          <div class="video-header">
            <h1>Video Segment Selector</h1>
            <button class="upload-new-btn" id="uploadNewBtn">📤 Upload New</button>
          </div>
          <div class="video-container">
            <video id="mainVideo" controls>
              <source src="/segments/${segments[0]?.filename || ''}" type="video/mp4">
            </video>
          </div>
        </div>
        
        <div class="segments-panel">
          <div class="toolbar">
            <button id="selectAll">Select All</button>
            <button id="selectNone">Deselect All</button>
            <button id="selectKeywords" class="primary">Select Keywords</button>
            <label>
              <input type="checkbox" id="showOnlyKeywords">
              Keyword Only
            </label>
            <button id="advancedToggle">⚙️ Advanced</button>
          </div>
          
          <div class="advanced-section" id="advancedSection">
            <div class="slider-control">
              <label>
                <span>Minimum Score Threshold</span>
                <span class="score-badge" id="scoreValue">7</span>
              </label>
              <input type="range" id="minScoreSlider" min="1" max="10" value="7" step="1">
            </div>
            <div style="margin-top: 12px;">
              <label style="display: block; margin-bottom: 6px; font-size: 13px;">
                AI instructions <span style="opacity: 0.6;">(required — describe what counts as a highlight)</span>
              </label>
              <textarea id="aiInstructions" rows="3"
                placeholder="e.g. Focus on goals, great saves and heated moments. Ignore casual banter and replays."
                style="width: 100%; box-sizing: border-box; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; padding: 8px; font-size: 13px; font-family: inherit; resize: vertical;"></textarea>
              <label style="display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 13px; cursor: pointer;">
                <input type="checkbox" id="rescoreCheck"> Re-run AI scoring (otherwise scores from the same instructions are reused)
              </label>
            </div>
            <div style="margin-top: 12px; border-top: 1px solid #3a3d45; padding-top: 12px;">
              <div style="display: flex; justify-content: space-between; align-items: center; gap: 10px;">
                <span id="llmStatus" style="font-size: 13px;">Checking AI backend...</span>
                <button id="llmSettingsToggle" style="padding: 6px 12px; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; cursor: pointer; font-size: 12px; white-space: nowrap;">AI Settings</button>
              </div>
              <div id="llmNotice" style="font-size: 12px; opacity: 0.75; margin-top: 6px; line-height: 1.4;"></div>
              <div id="llmSettingsPanel" style="display: none; margin-top: 10px; padding: 12px; background: #23262e; border: 1px solid #3a3d45; border-radius: 6px;">
                <div style="font-size: 12px; opacity: 0.7; margin-bottom: 10px;">
                  Scoring runs against a local model server if one is running (Ollama at
                  127.0.0.1:11434 by default), otherwise against the API below with your own key.
                </div>
                <label style="display: block; font-size: 12px; margin-bottom: 4px;">API base URL</label>
                <input id="llmBaseUrl" type="text" placeholder="https://api.openai.com/v1"
                  style="width: 100%; box-sizing: border-box; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; padding: 6px 8px; font-size: 12px; margin-bottom: 8px;">
                <label style="display: block; font-size: 12px; margin-bottom: 4px;">Model</label>
                <input id="llmModel" type="text" placeholder="gpt-4o-mini"
                  style="width: 100%; box-sizing: border-box; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; padding: 6px 8px; font-size: 12px; margin-bottom: 8px;">
                <label style="display: block; font-size: 12px; margin-bottom: 4px;">API key <span style="opacity: 0.6;">(stored on this machine only, never in the browser)</span></label>
                <input id="llmApiKey" type="password" autocomplete="off"
                  style="width: 100%; box-sizing: border-box; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; padding: 6px 8px; font-size: 12px; margin-bottom: 8px;">
                <label style="display: block; font-size: 12px; margin-bottom: 4px;">Local server URL</label>
                <input id="llmLocalUrl" type="text" placeholder="http://127.0.0.1:11434"
                  style="width: 100%; box-sizing: border-box; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; padding: 6px 8px; font-size: 12px; margin-bottom: 8px;">
                <label style="display: block; font-size: 12px; margin-bottom: 4px;">Local model</label>
                <input id="llmLocalModel" type="text" placeholder="qwen2.5:7b-instruct"
                  style="width: 100%; box-sizing: border-box; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; padding: 6px 8px; font-size: 12px; margin-bottom: 10px;">
                <div style="display: flex; gap: 8px;">
                  <button id="llmSaveBtn" style="flex: 1; padding: 8px; background: #5b7cff; border: 1px solid #5b7cff; color: white; border-radius: 6px; cursor: pointer; font-size: 12px;">Save</button>
                  <button id="llmTestBtn" style="flex: 1; padding: 8px; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; cursor: pointer; font-size: 12px;">Test Connection</button>
                </div>
                <div id="llmTestResult" style="font-size: 12px; margin-top: 8px;"></div>
              </div>
            </div>
            <div style="display: flex; gap: 10px; margin-top: 12px;">
              <button id="previewBtn" style="flex: 1; padding: 10px; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; cursor: pointer; font-size: 13px;">Preview Selection</button>
              <button id="resetSettingsBtn" style="padding: 10px 16px; background: #2a2d35; border: 1px solid #3a3d45; color: #e0e0e0; border-radius: 6px; cursor: pointer; font-size: 13px;">Default Settings</button>
            </div>
            <div class="preview-info" id="previewInfo"></div>
            <div class="download-row" id="autoDownloadRow">
              <a id="autoDownloadLink" class="download-link">Download Auto-Generated Reel</a>
              <button id="discardAutoBtn" class="discard-btn" title="Discard this reel to try a new prompt or pick clips manually">Discard</button>
            </div>
          </div>
          
          <div class="stats">
            <div class="stat-item">Total: <strong>${segments.length}</strong></div>
            <div class="stat-item">Selected: <strong id="selectedCount">0</strong></div>
            <div class="stat-item">Duration: <strong id="selectedDuration">0.0s</strong></div>
          </div>
          
          <div class="segments-list" id="segmentsList"></div>
          
          <div style="padding: 16px 20px; border-top: 1px solid #2a2d35; background: #1e2129;">
            <div style="display: flex; gap: 10px;">
              <button id="autoGenerateBtn" style="flex: 1; padding: 12px; background: #5b7cff; border: 1px solid #5b7cff; color: white; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500;">
                ✨ Auto-Generate Highlight Reel
              </button>
              <button id="exportBtn" style="flex: 1; padding: 12px; background: #28a745; border: 1px solid #28a745; color: white; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500; opacity: 0.3; cursor: not-allowed;" disabled>
                Create Highlight Reel
              </button>
            </div>
            <a id="downloadLink" class="download-link">Download Highlight Reel</a>
          </div>
        </div>
      </div>
    `;
  
    setupEventListeners(segments, videoId);
  }
  
  function setupEventListeners(segments: Segment[], videoId: string) {
    const segmentsList = document.getElementById("segmentsList")!;
    const mainVideo = document.getElementById("mainVideo") as HTMLVideoElement;
    const exportBtn = document.getElementById("exportBtn") as HTMLButtonElement;
    const downloadLink = document.getElementById("downloadLink") as HTMLAnchorElement;
    const autoGenerateBtn = document.getElementById("autoGenerateBtn") as HTMLButtonElement;
    const autoDownloadLink = document.getElementById("autoDownloadLink") as HTMLAnchorElement;
    const autoDownloadRow = document.getElementById("autoDownloadRow")!;
    const discardAutoBtn = document.getElementById("discardAutoBtn") as HTMLButtonElement;
    const advancedToggle = document.getElementById("advancedToggle") as HTMLButtonElement;
    const advancedSection = document.getElementById("advancedSection")!;
    const previewBtn = document.getElementById("previewBtn") as HTMLButtonElement;
    const previewInfo = document.getElementById("previewInfo")!;
    const minScoreSlider = document.getElementById("minScoreSlider") as HTMLInputElement;
    const scoreValue = document.getElementById("scoreValue")!;
    const resetSettingsBtn = document.getElementById("resetSettingsBtn") as HTMLButtonElement;
    const selectedCountEl = document.getElementById("selectedCount")!;
    const selectedDurationEl = document.getElementById("selectedDuration")!;
    const uploadNewBtn = document.getElementById("uploadNewBtn") as HTMLButtonElement;
  
    const llmStatus = document.getElementById("llmStatus")!;
    const llmNotice = document.getElementById("llmNotice")!;
    const llmSettingsToggle = document.getElementById("llmSettingsToggle") as HTMLButtonElement;
    const llmSettingsPanel = document.getElementById("llmSettingsPanel")!;
    const llmBaseUrl = document.getElementById("llmBaseUrl") as HTMLInputElement;
    const llmModel = document.getElementById("llmModel") as HTMLInputElement;
    const llmApiKey = document.getElementById("llmApiKey") as HTMLInputElement;
    const llmLocalUrl = document.getElementById("llmLocalUrl") as HTMLInputElement;
    const llmLocalModel = document.getElementById("llmLocalModel") as HTMLInputElement;
    const llmSaveBtn = document.getElementById("llmSaveBtn") as HTMLButtonElement;
    const llmTestBtn = document.getElementById("llmTestBtn") as HTMLButtonElement;
    const llmTestResult = document.getElementById("llmTestResult")!;

    const selectedIds = new Set<number>();
    let currentSegmentId: number | null = segments.length > 0 ? segments[0].id : null;
    let llmAvailable = false;

    function setAutoGenerateEnabled(enabled: boolean) {
      autoGenerateBtn.disabled = !enabled;
      autoGenerateBtn.style.opacity = enabled ? "1" : "0.5";
      autoGenerateBtn.style.cursor = enabled ? "pointer" : "not-allowed";
    }

    async function refreshLlmStatus() {
      try {
        const response = await fetch("/api/llm-settings");
        const s = await response.json();
        llmBaseUrl.value = s.base_url || "";
        llmModel.value = s.model || "";
        llmLocalUrl.value = s.local_url || "";
        llmLocalModel.value = s.local_model || "";
        llmApiKey.placeholder = s.has_api_key ? "(saved - enter a new key to replace)" : "";

        llmAvailable = !!s.resolved_backend;
        setAutoGenerateEnabled(llmAvailable);

        if (s.resolved_backend === "local") {
          llmStatus.textContent = `AI backend: local model (${s.local_model})`;
          llmNotice.textContent = "Scoring runs entirely on this machine. Nothing is sent over the network.";
        } else if (s.resolved_backend === "api") {
          let host = s.base_url;
          try { host = new URL(s.base_url).host; } catch {}
          llmStatus.textContent = `AI backend: API (${s.model})`;
          llmNotice.textContent = `Auto-Generate sends your transcript text to ${host} using your API key. Your video and audio never leave your machine.`;
        } else if (s.resolved_backend === "g4f") {
          llmStatus.textContent = "AI backend: g4f (development only)";
          llmNotice.textContent = "g4f relays your transcript to unpredictable third-party endpoints. Development use only; never use it with private recordings.";
        } else {
          llmStatus.textContent = "AI backend: not configured";
          llmNotice.textContent = "Auto-Generate is disabled. Run a local model server (e.g. Ollama) or add an API key under AI Settings. Manual clip selection always works, fully offline.";
        }
      } catch {
        llmStatus.textContent = "AI backend: status unavailable";
      }
    }

    llmSettingsToggle.addEventListener("click", () => {
      const isOpen = llmSettingsPanel.style.display !== "none";
      llmSettingsPanel.style.display = isOpen ? "none" : "block";
      advancedSection.classList.add("visible");
    });

    llmSaveBtn.addEventListener("click", async () => {
      const payload: Record<string, string> = {
        base_url: llmBaseUrl.value.trim(),
        model: llmModel.value.trim(),
        local_url: llmLocalUrl.value.trim(),
        local_model: llmLocalModel.value.trim(),
      };
      // A blank key field means "keep the saved key", not "clear it".
      if (llmApiKey.value.trim()) payload.api_key = llmApiKey.value.trim();

      llmSaveBtn.disabled = true;
      llmSaveBtn.textContent = "Saving...";
      try {
        const response = await fetch("/api/llm-settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const result = await response.json();
        if (result.status === "ok") {
          llmApiKey.value = "";
          llmTestResult.textContent = "Settings saved.";
          llmTestResult.style.color = "#7ee08a";
          await refreshLlmStatus();
        } else {
          llmTestResult.textContent = `Error: ${result.message || "could not save settings"}`;
          llmTestResult.style.color = "#ff8a8a";
        }
      } catch (err) {
        console.error(err);
        llmTestResult.textContent = "Error: could not save settings";
        llmTestResult.style.color = "#ff8a8a";
      } finally {
        llmSaveBtn.disabled = false;
        llmSaveBtn.textContent = "Save";
      }
    });

    llmTestBtn.addEventListener("click", async () => {
      llmTestBtn.disabled = true;
      llmTestBtn.textContent = "Testing...";
      llmTestResult.textContent = "Contacting the model...";
      llmTestResult.style.color = "#e0e0e0";
      try {
        const response = await fetch("/api/llm-test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        });
        const result = await response.json();
        if (result.status === "ok") {
          llmTestResult.textContent = `Connected: ${result.backend} backend, model ${result.model}.`;
          llmTestResult.style.color = "#7ee08a";
        } else {
          llmTestResult.textContent = `Failed: ${result.message}`;
          llmTestResult.style.color = "#ff8a8a";
        }
      } catch (err) {
        console.error(err);
        llmTestResult.textContent = "Failed: could not reach the backend server.";
        llmTestResult.style.color = "#ff8a8a";
      } finally {
        llmTestBtn.disabled = false;
        llmTestBtn.textContent = "Test Connection";
      }
    });

    refreshLlmStatus();
  
    if (segments.length > 0) {
      mainVideo.src = `/segments/${segments[0].filename}`;
    }
  
    uploadNewBtn.addEventListener("click", () => {
      if (confirm("Upload a new video? Current progress will be lost.")) {
        // Clear the current view and show upload interface
        showUploadInterface();
      }
    });
  
    function updateVideoForSegment(segmentId: number) {
      const segment = segments.find(s => s.id === segmentId);
      if (segment) {
        currentSegmentId = segmentId;
        mainVideo.src = `/segments/${segment.filename}`;
        mainVideo.currentTime = 0;
      }
    }
  
    function formatTime(seconds: number): string {
      const mins = Math.floor(seconds / 60);
      const secs = Math.floor(seconds % 60);
      return `${mins}:${secs.toString().padStart(2, '0')}`;
    }
  
    function updateStats() {
      selectedCountEl.textContent = selectedIds.size.toString();
      const totalDur = segments
        .filter(s => selectedIds.has(s.id))
        .reduce((a, s) => a + s.duration, 0);
      selectedDurationEl.textContent = totalDur.toFixed(1) + "s";
      
      exportBtn.disabled = selectedIds.size === 0;
      if (selectedIds.size === 0) {
        exportBtn.style.opacity = "0.3";
        exportBtn.style.cursor = "not-allowed";
      } else {
        exportBtn.style.opacity = "1";
        exportBtn.style.cursor = "pointer";
      }
    }
  
    function renderSegments(filterKeywords = false) {
      segmentsList.innerHTML = "";
      segments.forEach(seg => {
        if (filterKeywords && !seg.is_keyword) return;
  
        const div = document.createElement("div");
        div.className = "segment-row" + 
          (seg.is_keyword ? " keyword" : "") +
          (selectedIds.has(seg.id) ? " selected" : "") +
          (currentSegmentId === seg.id ? " active" : "");
        
        div.innerHTML = `
          <div class="segment-id">${seg.id}</div>
          <div class="segment-time">${formatTime(seg.start)}</div>
          <div class="segment-time">${formatTime(seg.end)}</div>
          <div class="segment-text">${seg.text}</div>
          <div class="segment-info">
            ${seg.is_keyword ? '<span class="segment-score">CPS: High</span>' : ''}
            <span>Duration: ${seg.duration.toFixed(1)}s</span>
          </div>
          <div class="segment-actions">
            <input type="checkbox" class="segment-checkbox" data-id="${seg.id}" ${selectedIds.has(seg.id) ? "checked" : ""}>
            <button class="icon-btn play-btn" data-id="${seg.id}" title="Play segment">▶</button>
          </div>
        `;
        
        const checkbox = div.querySelector(".segment-checkbox") as HTMLInputElement;
        checkbox.addEventListener("change", (e) => {
          e.stopPropagation();
          if (checkbox.checked) selectedIds.add(seg.id);
          else selectedIds.delete(seg.id);
          div.classList.toggle("selected", checkbox.checked);
          updateStats();
        });
  
        const playBtn = div.querySelector(".play-btn") as HTMLButtonElement;
        playBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          updateVideoForSegment(seg.id);
          mainVideo.play();
          document.querySelectorAll('.segment-row').forEach(row => row.classList.remove('active'));
          div.classList.add('active');
        });
  
        div.addEventListener("click", (e) => {
          if (!(e.target as HTMLElement).closest(".segment-checkbox, .play-btn")) {
            updateVideoForSegment(seg.id);
            document.querySelectorAll('.segment-row').forEach(row => row.classList.remove('active'));
            div.classList.add('active');
            
            checkbox.checked = !checkbox.checked;
            checkbox.dispatchEvent(new Event("change"));
          }
        });
  
        segmentsList.appendChild(div);
      });
    }
  
    renderSegments();
  
    document.getElementById("selectAll")!.addEventListener("click", () => {
      segments.forEach(s => selectedIds.add(s.id));
      renderSegments((document.getElementById("showOnlyKeywords") as HTMLInputElement).checked);
      updateStats();
    });
  
    document.getElementById("selectNone")!.addEventListener("click", () => {
      selectedIds.clear();
      renderSegments((document.getElementById("showOnlyKeywords") as HTMLInputElement).checked);
      updateStats();
    });
  
    document.getElementById("selectKeywords")!.addEventListener("click", () => {
      selectedIds.clear();
      segments.filter(s => s.is_keyword).forEach(s => selectedIds.add(s.id));
      renderSegments((document.getElementById("showOnlyKeywords") as HTMLInputElement).checked);
      updateStats();
    });
  
    document.getElementById("showOnlyKeywords")!.addEventListener("change", (e) => {
      renderSegments((e.target as HTMLInputElement).checked);
    });
  
    advancedToggle.addEventListener("click", () => {
      advancedSection.classList.toggle("visible");
    });
  
    minScoreSlider.addEventListener("input", () => {
      scoreValue.textContent = minScoreSlider.value;
      previewInfo.classList.remove("visible");
    });
  
    resetSettingsBtn.addEventListener("click", () => {
      minScoreSlider.value = "7";
      scoreValue.textContent = "7";
      previewInfo.classList.remove("visible");
    });
  
    previewBtn.addEventListener("click", async () => {
      const minScore = parseInt(minScoreSlider.value);
      previewBtn.disabled = true;
      previewBtn.style.opacity = "0.5";
      previewBtn.textContent = "Loading...";
      
      try {
        const response = await fetch("/api/preview-auto-export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ min_score: minScore })
        });
        const result = await response.json();
        
        if (result.status === "ok") {
          const duration = Math.round(result.estimated_duration_seconds);
          previewInfo.innerHTML = `
            📊 ${result.count} highlights (of ${result.total_available} available)<br>
            ⏱️ Duration: ${duration}s (${Math.floor(duration/60)}:${(duration%60).toString().padStart(2,'0')})<br>
            🎯 Threshold: ${minScore}+
          `;
          previewInfo.classList.add("visible");
        } else {
          alert(`Error: ${result.message}`);
        }
      } catch (err) {
        console.error(err);
        alert("Failed to load preview");
      } finally {
        previewBtn.disabled = false;
        previewBtn.style.opacity = "1";
        previewBtn.textContent = "Preview Selection";
      }
    });
  
    autoGenerateBtn.addEventListener("click", async () => {
      if (!llmAvailable) {
        advancedSection.classList.add("visible");
        alert("No AI backend is configured. Run a local model server (e.g. Ollama) or add an API key under AI Settings.");
        return;
      }
      const useScoreFilter = advancedSection.classList.contains("visible");
      const minScore = useScoreFilter ? parseInt(minScoreSlider.value) : 0;
      const instructionsInput = document.getElementById("aiInstructions") as HTMLTextAreaElement;
      const instructions = instructionsInput.value.trim();
      const rescore = (document.getElementById("rescoreCheck") as HTMLInputElement).checked;

      if (!instructions) {
        advancedSection.classList.add("visible");
        alert("Please describe what counts as a highlight before auto-generating.");
        instructionsInput.focus();
        return;
      }

      autoGenerateBtn.disabled = true;
      autoGenerateBtn.style.opacity = "0.5";
      autoGenerateBtn.style.cursor = "not-allowed";
      autoGenerateBtn.textContent = "Starting...";
      autoDownloadRow.classList.remove("visible");

      try {
        const response = await fetch("/api/auto-generate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            min_score: minScore,
            sort_chronological: true,
            instructions: instructions,
            rescore: rescore
          })
        });
        const result = await response.json();

        if (result.status !== "ok" || !result.task_id) {
          alert(`Error: ${result.message || "Failed to start auto-generation"}`);
          return;
        }

        // Poll the task: AI scoring can take a minute or two
        const deadline = Date.now() + 15 * 60 * 1000;
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 3000));
          const statusResponse = await fetch(`/api/processing-status/${result.task_id}`);
          const status = await statusResponse.json();

          if (status.status === "complete") {
            autoDownloadLink.href = status.output_file;
            autoDownloadLink.target = "_blank";
            autoDownloadRow.classList.add("visible");
            autoDownloadLink.textContent = `✅ Download (${status.highlights_used} clips, ${status.file_size_mb} MB)`;

            const filterMsg = useScoreFilter ? ` (score >= ${minScore})` : "";
            alert(`Ready! ${status.highlights_used} of ${status.total_available} highlights${filterMsg}.`);
            return;
          }
          if (status.status === "error") {
            alert(`Error: ${status.message}`);
            return;
          }
          autoGenerateBtn.textContent = status.message || "Working...";
        }
        alert("Timed out waiting for the highlight reel. Check the app logs.");
      } catch (err) {
        console.error(err);
        alert("Failed to auto-generate highlight reel");
      } finally {
        setAutoGenerateEnabled(llmAvailable);
        autoGenerateBtn.textContent = "✨ Auto-Generate Highlight Reel";
      }
    });
  
    discardAutoBtn.addEventListener("click", async () => {
      if (!confirm("Discard this auto-generated reel? You can then try a new prompt or pick clips manually.")) return;

      const href = autoDownloadLink.getAttribute("href");
      autoDownloadRow.classList.remove("visible");
      autoDownloadLink.removeAttribute("href");

      if (href) {
        // Best-effort server-side cleanup; the UI is already reset.
        try {
          await fetch("/api/discard-reel", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ output_file: href })
          });
        } catch (err) {
          console.error("Failed to delete reel on server:", err);
        }
      }
    });

    exportBtn.addEventListener("click", async () => {
      const payload = { selected_ids: Array.from(selectedIds) };
      exportBtn.disabled = true;
      exportBtn.style.opacity = "0.5";
      exportBtn.style.cursor = "not-allowed";
      exportBtn.textContent = "Generating...";
      try {
        const response = await fetch("/api/export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const result = await response.json();
        if (result.status === "ok") {
          downloadLink.href = result.output_file;
          downloadLink.target = "_blank";
          downloadLink.classList.add("visible");
          downloadLink.textContent = "✅ Download Highlight Reel";
        } else {
          alert(`Error: ${result.message}`);
        }
      } catch (err) {
        console.error(err);
        alert("Failed to send selected segments");
      } finally {
        exportBtn.disabled = false;
        exportBtn.style.opacity = "1";
        exportBtn.style.cursor = "pointer";
        exportBtn.textContent = "Create Highlight Reel";
      }
    });
  }
  
  main();