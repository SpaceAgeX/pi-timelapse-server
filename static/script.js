const cameraStatus = document.querySelector("#camera-status");
const cameraFeed = document.querySelector("#camera-feed");
const cameraError = document.querySelector("#camera-error");

const timelapseState = document.querySelector("#timelapse-state");

const printNameInput = document.querySelector("#print-name");
const intervalSelect = document.querySelector("#interval");
const durationSelect = document.querySelector("#duration");
const deleteFramesCheckbox = document.querySelector("#delete-frames");

const startButton = document.querySelector("#start-button");
const stopButton = document.querySelector("#stop-button");
const refreshRecordingsButton = document.querySelector(
    "#refresh-recordings",
);

const messageElement = document.querySelector("#message");

const elapsedValue = document.querySelector("#elapsed-value");
const remainingValue = document.querySelector("#remaining-value");
const frameCountValue = document.querySelector("#frame-count");
const sessionSizeValue = document.querySelector("#session-size");
const freeStorageValue = document.querySelector("#free-storage");

const recordingsList = document.querySelector("#recordings-list");

let previousTimelapseState = null;


async function apiRequest(url, options = {}) {
    const response = await fetch(url, {
        cache: "no-store",
        ...options,
        headers: {
            "Content-Type": "application/json",
            ...(options.headers || {}),
        },
    });

    if (!response.ok) {
        let message = `HTTP ${response.status}`;

        try {
            const body = await response.json();
            message = body.detail || message;
        } catch {
            // Use the generic message.
        }

        throw new Error(message);
    }

    return response.json();
}


function formatDuration(totalSeconds) {
    if (
        totalSeconds === null
        || totalSeconds === undefined
        || !Number.isFinite(totalSeconds)
    ) {
        return "—";
    }

    const seconds = Math.max(
        0,
        Math.floor(totalSeconds),
    );

    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(
        (seconds % 3600) / 60,
    );
    const remainingSeconds = seconds % 60;

    return [
        hours,
        minutes,
        remainingSeconds,
    ]
        .map((value) => String(value).padStart(2, "0"))
        .join(":");
}


function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) {
        return "0 MB";
    }

    const units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ];

    const index = Math.min(
        Math.floor(Math.log(bytes) / Math.log(1024)),
        units.length - 1,
    );

    const value = bytes / (1024 ** index);

    return `${value.toFixed(index >= 3 ? 2 : 1)} ${units[index]}`;
}


function formatDate(timestampSeconds) {
    return new Date(
        timestampSeconds * 1000,
    ).toLocaleString();
}


function showMessage(text, type = "") {
    messageElement.textContent = text;
    messageElement.className = `message ${type}`;
}


function updateCameraStatus(camera) {
    if (camera.connected) {
        cameraStatus.textContent = "Camera online";
        cameraStatus.className = "status status-online";
    } else {
        cameraStatus.textContent = "Camera offline";
        cameraStatus.className = "status status-offline";
    }
}


function updateTimelapseStatus(timelapse) {
    const state = timelapse.state;

    const stateLabels = {
        idle: "Idle",
        recording: "Recording",
        stopping: "Stopping",
        encoding: "Creating video",
        complete: "Complete",
        error: "Error",
    };

    timelapseState.textContent = (
        stateLabels[state] || state
    );

    timelapseState.className = (
        `state-badge ${state}`
    );

    elapsedValue.textContent = formatDuration(
        timelapse.elapsed_seconds,
    );

    remainingValue.textContent = formatDuration(
        timelapse.remaining_seconds,
    );

    frameCountValue.textContent = String(
        timelapse.frame_count || 0,
    );

    sessionSizeValue.textContent = formatBytes(
        timelapse.session_size_bytes || 0,
    );

    const isActive = [
        "recording",
        "stopping",
        "encoding",
    ].includes(state);

    const canStop = state === "recording";

    startButton.disabled = isActive;
    stopButton.disabled = !canStop;

    printNameInput.disabled = isActive;
    intervalSelect.disabled = isActive;
    durationSelect.disabled = isActive;
    deleteFramesCheckbox.disabled = isActive;

    if (state !== previousTimelapseState) {
        if (state === "recording") {
            showMessage(
                "Timelapse recording started.",
                "success",
            );
        }

        if (state === "stopping") {
            showMessage(
                "Stopping capture and preparing the video…",
            );
        }

        if (state === "encoding") {
            showMessage(
                "FFmpeg is creating the MP4. Do not shut down the Pi.",
            );
        }

        if (state === "complete") {
            showMessage(
                "Video complete. It is ready to download below.",
                "success",
            );

            loadRecordings();
        }

        if (state === "error") {
            showMessage(
                timelapse.error || "Timelapse failed.",
                "error",
            );
        }

        previousTimelapseState = state;
    }
}


async function loadStatus() {
    try {
        const status = await apiRequest("/api/status");

        updateCameraStatus(status.camera);
        updateTimelapseStatus(status.timelapse);

        freeStorageValue.textContent = formatBytes(
            status.storage.free_bytes,
        );
    } catch (error) {
        console.error(error);

        cameraStatus.textContent = "Server offline";
        cameraStatus.className = "status status-offline";
    }
}


async function startTimelapse() {
    startButton.disabled = true;

    const durationValue = Number(
        durationSelect.value,
    );

    const requestBody = {
        name: printNameInput.value.trim() || "3D Print",
        interval_seconds: Number(
            intervalSelect.value,
        ),
        duration_hours: (
            durationValue === 0
                ? null
                : durationValue
        ),
        delete_frames_after_encoding: (
            deleteFramesCheckbox.checked
        ),
    };

    try {
        const timelapse = await apiRequest(
            "/api/timelapse/start",
            {
                method: "POST",
                body: JSON.stringify(requestBody),
            },
        );

        updateTimelapseStatus(timelapse);
    } catch (error) {
        console.error(error);
        showMessage(error.message, "error");
        startButton.disabled = false;
    }
}


async function stopTimelapse() {
    stopButton.disabled = true;

    try {
        const timelapse = await apiRequest(
            "/api/timelapse/stop",
            {
                method: "POST",
            },
        );

        updateTimelapseStatus(timelapse);
    } catch (error) {
        console.error(error);
        showMessage(error.message, "error");
        stopButton.disabled = false;
    }
}


async function loadRecordings() {
    try {
        const result = await apiRequest(
            "/api/recordings",
        );

        recordingsList.replaceChildren();

        if (result.recordings.length === 0) {
            const emptyMessage = document.createElement("p");
            emptyMessage.className = "empty-message";
            emptyMessage.textContent = (
                "No completed recordings yet."
            );

            recordingsList.append(emptyMessage);
            return;
        }

        for (const recording of result.recordings) {
            const item = document.createElement("article");
            item.className = "recording-item";

            const info = document.createElement("div");
            info.className = "recording-info";

            const name = document.createElement("p");
            name.className = "recording-name";
            name.textContent = recording.filename;

            const metadata = document.createElement("p");
            metadata.className = "recording-meta";
            metadata.textContent = (
                `${formatBytes(recording.size_bytes)}`
                + ` • ${formatDate(recording.created_at)}`
            );

            const download = document.createElement("a");
            download.className = "download-button";
            download.href = recording.download_url;
            download.textContent = "Download MP4";
            download.setAttribute(
                "download",
                recording.filename,
            );

            info.append(name, metadata);
            item.append(info, download);
            recordingsList.append(item);
        }
    } catch (error) {
        console.error(error);

        recordingsList.innerHTML = `
            <p class="empty-message">
                Could not load recordings.
            </p>
        `;
    }
}


cameraFeed.addEventListener("load", () => {
    cameraError.classList.add("hidden");
});


cameraFeed.addEventListener("error", () => {
    cameraError.classList.remove("hidden");
});


startButton.addEventListener(
    "click",
    startTimelapse,
);


stopButton.addEventListener(
    "click",
    stopTimelapse,
);


refreshRecordingsButton.addEventListener(
    "click",
    loadRecordings,
);


loadStatus();
loadRecordings();

window.setInterval(loadStatus, 2000);
window.setInterval(loadRecordings, 30000);