const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect', function() {
    console.log("SUCCESS: Connected to Flask WebSocket Server!");
});

socket.on('disconnect', function() {
    console.log("WARNING: Disconnected from WebSocket Server!");
});

// Hardware Status UI
socket.on('hardware_status_update', function(status) {
    const wifiLed = document.getElementById('wifi_led');
    if (wifiLed) {
        if (status.wifi_sniffer === "receiving") {
            wifiLed.style.color = '#10b981'; 
            wifiLed.innerText = '● Connected / Receiving';
        } else if (status.wifi_sniffer === "waiting") {
            wifiLed.style.color = '#f59e0b'; 
            wifiLed.innerText = '● Listening (Waiting for Data)';
        } else {
            wifiLed.style.color = '#ef4444'; 
            wifiLed.innerText = '● Disconnected';
        }
    }
});

// MQTT Status UI
socket.on('mqtt_status', function(data) {
    let mqttLed = document.getElementById('mqtt_led');
    if (mqttLed) {
        if (data.status === 'receiving') {
            mqttLed.innerText = '● Connected / Receiving';
            mqttLed.style.color = '#10b981'; 
        } else if (data.status === 'waiting') {
            mqttLed.innerText = '● Connected (Waiting for Data)';
            mqttLed.style.color = '#f59e0b'; 
        } else {
            mqttLed.innerText = '● Disconnected';
            mqttLed.style.color = '#ef4444'; 
        }
    }
});

const defaultLat = 62.693;
const defaultLng = 22.824;
const map = L.map('map').setView([defaultLat, defaultLng], 14);

const tileLayers = {
    google_normal: L.tileLayer('https://{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', { maxZoom: 20, subdomains: ['mt0', 'mt1', 'mt2', 'mt3'] }),
    google_satellite: L.tileLayer('https://{s}.google.com/vt/lyrs=s,h&x={x}&y={y}&z={z}', { maxZoom: 20, subdomains: ['mt0', 'mt1', 'mt2', 'mt3'] }),
    osm: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 })
};

tileLayers.google_normal.addTo(map);

function switchMapLayer(layerKey) {
    Object.values(tileLayers).forEach(layer => map.removeLayer(layer));
    if (tileLayers[layerKey]) {
        tileLayers[layerKey].addTo(map);
    }
}

let markers = {};
let droneLatestData = {};
let trajectories = {};
let activeSelectedId = null;
let currentTrajectoryLine = null;
let geofences = [];

let ignoredWarnings = {}; 
let warningLogsHistory = []; 
let blacklistLogged = {}; 

const alertAudio = new Audio('https://media.geeksforgeeks.org/wp-content/uploads/20190531135120/beep.mp3');

function isPointInPolygon(point, vs) {
    const x = point[0], y = point[1];
    let inside = false;
    for (let i = 0, j = vs.length - 1; i < vs.length; j = i++) {
        const xi = vs[i][0], yi = vs[i][1];
        const xj = vs[j][0], yj = vs[j][1];
        const intersect = ((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi);
        if (intersect) inside = !inside;
    }
    return inside;
}

function createLuminousArrowIcon(last4, heading) {
    const rot = heading || 0;
    const svgHtml = `
        <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; pointer-events: auto; cursor: pointer;">
            <svg width="28" height="28" viewBox="0 0 24 24" style="transform: rotate(${rot}deg); transition: transform 0.3s ease; filter: drop-shadow(0px 0px 6px #10b981);">
                <path d="M12 2L2 22L12 18L22 22L12 2Z" fill="#10b981" stroke="#ffffff" stroke-width="1.5" stroke-linejoin="round"/>
            </svg>
            <div style="background: rgba(15, 23, 42, 0.9); color: #10b981; font-weight: bold; font-size: 10px; border: 1px solid #10b981; border-radius: 3px; padding: 1px 4px; margin-top: 2px; white-space: nowrap; box-shadow: 0 2px 4px rgba(0,0,0,0.6);">
                ${last4}
            </div>
        </div>
    `;
    return L.divIcon({
        className: 'custom-drone-arrow-icon',
        html: svgHtml,
        iconSize: [40, 50],
        iconAnchor: [20, 25]
    });
}

function getStorageGeofences() {
    return geofences.map(g => {
        const { layer, ...rest } = g;
        return rest;
    });
}

// Load Local Storage Geofences
function loadGeofences() {
    const stored = localStorage.getItem('dms_geofences');
    if (stored) {
        try {
            const savedGeofences = JSON.parse(stored);
            const now = new Date().getTime();
            geofences = [];
            let hasExpired = false;

            savedGeofences.forEach(gf => {
                let isExpired = false;
                
                if (!gf.never_expires && gf.end_time) {
                    const expTime = new Date(gf.end_time).getTime();
                    if (now > expTime) isExpired = true;
                }

                if (!isExpired) {
                    let fillColor = "#ef4444";
                    if (gf.mode === "above") fillColor = "#f59e0b";
                    if (gf.mode === "below") fillColor = "#3b82f6";

                    const polygon = L.polygon(gf.coords, { 
                        color: fillColor, 
                        fillColor: fillColor, 
                        fillOpacity: 0.35 
                    }).addTo(map);

                    polygon.bindPopup(`<b>${gf.name}</b><br>Mode: ${gf.mode}<br>Alt Limit: ${gf.alt_limit || 0}m`);
                    gf.layer = polygon; 
                    geofences.push(gf);
                } else {
                    hasExpired = true; 
                }
            });

            if (hasExpired) {
                localStorage.setItem('dms_geofences', JSON.stringify(getStorageGeofences()));
            }

        } catch (e) {
            console.error("Failed to load local geofences", e);
        }
    }
}

loadGeofences();

// Real-time Expiration Checker (Runs every second)
setInterval(function() {
    const now = new Date().getTime();
    let changed = false;

    geofences = geofences.filter(gf => {
        if (!gf.never_expires && gf.end_time) {
            const expTime = new Date(gf.end_time).getTime();
            if (now > expTime) {
                if (gf.layer) map.removeLayer(gf.layer);
                changed = true;
                return false;
            }
        }
        return true;
    });

    if (changed) {
        localStorage.setItem('dms_geofences', JSON.stringify(getStorageGeofences()));
    }
}, 1000);

// Dynamic Toast Container Helper
function getToastContainer() {
    let container = document.getElementById('warningToastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = 'warningToastContainer';
        container.style.cssText = "position:fixed; bottom:50px; right:20px; z-index:2000; display:flex; flex-direction:column; gap:10px; max-height:80vh; overflow-y:auto; pointer-events:none;";
        document.body.appendChild(container);
    }
    return container;
}

// Display Stacked Warnings
function showWarningToast(msg, warningKey) {
    const container = getToastContainer();

    let toast = document.getElementById(`toast_${warningKey}`);
    if (!toast) {
        toast = document.createElement('div');
        toast.id = `toast_${warningKey}`;
        toast.style.cssText = "background:rgba(239, 68, 68, 0.95); color:white; padding:8px 14px; border-radius:6px; font-size:0.85rem; border:1px solid #f87171; box-shadow:0 4px 12px rgba(0,0,0,0.4); display:flex; align-items:center; gap:10px; pointer-events:auto;";
        
        toast.innerHTML = `
            <span class="toast-text">${msg}</span>
            <button onclick="ignoreWarning('${warningKey}')" style="background:#0f172a; color:#f8fafc; border:1px solid #475569; padding:2px 8px; border-radius:4px; cursor:pointer; font-size:0.75rem;">Ignore / Mute</button>
        `;
        
        container.prepend(toast);
    } else {
        const textSpan = toast.querySelector('.toast-text');
        if (textSpan) textSpan.innerText = msg;
    }
}

// Manual Ignore / Mute Action
function ignoreWarning(warningKey) {
    if (warningKey) {
        ignoredWarnings[warningKey] = true;
        removeWarningToast(warningKey);
    }
}

// Helper to remove toast
function removeWarningToast(warningKey) {
    const toast = document.getElementById(`toast_${warningKey}`);
    if (toast) {
        toast.remove();
    }
    
    const container = document.getElementById('warningToastContainer');
    if (container && container.children.length === 0) {
        alertAudio.pause();
        alertAudio.currentTime = 0;
    }
}

// Telemetry Logic
socket.on('telemetry_update', function(data) {
    const devId = data.device_id;
    const lat = parseFloat(data.latitude);
    const lng = parseFloat(data.longitude);
    const heading = parseFloat(data.heading) || 0;
    const last4 = devId.slice(-4);

    droneLatestData[devId] = data;

    if (!trajectories[devId]) {
        trajectories[devId] = [];
    }
    trajectories[devId].push([lat, lng]);

    if (!markers[devId]) {
        const icon = createLuminousArrowIcon(last4, heading);
        const newMarker = L.marker([lat, lng], { icon: icon }).addTo(map);
        newMarker.on('click', () => selectDrone(devId));
        markers[devId] = newMarker;

        if (!activeSelectedId) {
            selectDrone(devId);
        }
    } else {
        markers[devId].setLatLng([lat, lng]);
        markers[devId].setIcon(createLuminousArrowIcon(last4, heading));
    }

    // Geofence Intersection check
    geofences.forEach(gf => {
        const warningKey = `${devId}_${gf.name}`;
        if (isPointInPolygon([lat, lng], gf.coords)) {
            const nowTime = new Date().toLocaleTimeString();
            const logEntry = `[${nowTime}] Drone '${devId}' entered '${gf.name}'`;
            if (!warningLogsHistory.includes(logEntry)) {
                warningLogsHistory.unshift(logEntry);
            }

            if (!ignoredWarnings[warningKey]) {
                alertAudio.play().catch(e => console.log(e));
                showWarningToast(`🚨 Drone ${devId} is inside restricted '${gf.name}'!`, warningKey);
            }
        } else {
            if (!ignoredWarnings[warningKey]) {
                removeWarningToast(warningKey);
            }
        }
    });

    // Blacklist check
    if (data.blacklisted) {
        const bKey = `${devId}_blacklist`;
        if (!blacklistLogged[devId]) {
            const nowTime = new Date().toLocaleTimeString();
            warningLogsHistory.unshift(`[${nowTime}] Blacklisted drone '${devId}' detected!`);
            blacklistLogged[devId] = true;
        }
        if (!ignoredWarnings[bKey]) {
            alertAudio.play().catch(e => console.log(e));
            showWarningToast(`🚨 Blacklisted drone ${devId} detected!`, bKey);
        }
    }

    if (activeSelectedId === devId) {
        updateTelemetryCard(data);
        if (currentTrajectoryLine) {
            currentTrajectoryLine.setLatLngs(trajectories[devId]);
        }
    }
});

function selectDrone(devId) {
    activeSelectedId = devId;
    const data = droneLatestData[devId];

    if (data) {
        updateTelemetryCard(data);
    }

    if (currentTrajectoryLine) {
        map.removeLayer(currentTrajectoryLine);
        currentTrajectoryLine = null;
    }

    if (trajectories[devId] && trajectories[devId].length > 0) {
        currentTrajectoryLine = L.polyline(trajectories[devId], {
            color: '#10b981',
            weight: 3,
            opacity: 0.85,
            dashArray: '4, 4'
        }).addTo(map);
    }
}

function updateTelemetryCard(data) {
    document.getElementById('disp_id').innerText = data.device_id;
    document.getElementById('disp_model').innerText = data.model || 'Unknown';
    document.getElementById('disp_source').innerText = data.source;
    document.getElementById('disp_pos').innerText = `${data.latitude}, ${data.longitude}`;
    document.getElementById('disp_alt').innerText = `${data.altitude} m`;
    document.getElementById('disp_spd').innerText = `${data.speed} km/h`;
    document.getElementById('disp_hdg').innerText = `${data.heading}°`;
}

function openModal(modalId) {
    document.getElementById(modalId).style.display = "block";
}

function closeModal(modalId) {
    document.getElementById(modalId).style.display = "none";
}

function openWarningModal() {
    const container = document.getElementById('warningLogsContainer');
    container.innerHTML = '';
    
    if (warningLogsHistory.length === 0) {
        container.innerHTML = '<span style="color:#94a3b8;">No warning logs recorded yet.</span>';
    } else {
        warningLogsHistory.forEach(log => {
            const div = document.createElement('div');
            div.style.padding = '5px 0';
            div.style.borderBottom = '1px solid #334155';
            div.style.color = '#ef4444';
            div.innerText = log;
            container.appendChild(div);
        });
    }
    openModal('warningModal');
}

function openUserModal() {
    loadUsersList();
    openModal('userModal');
}

// Save Geofence
function saveGeofence() {
    const name = document.getElementById('gf_name').value.trim() || "area1";
    const coordsStr = document.getElementById('gf_coords').value;
    const mode = document.getElementById('gf_vert_mode').value;
    const altLimit = document.getElementById('gf_alt_limit').value || 0;

    const endTimeInput = document.getElementById('gf_end');
    const neverExpiresInput = document.getElementById('gf_never_expires');

    let endTime = null;
    let neverExpires = true; 

    if (neverExpiresInput) {
        neverExpires = neverExpiresInput.checked;
    }
    if (endTimeInput && endTimeInput.value) {
        endTime = endTimeInput.value;
    }

    if (!coordsStr) return alert("Please enter GPS coordinates!");

    const coordPairs = coordsStr.split(';').map(pair => {
        const parts = pair.split(',');
        return [parseFloat(parts[0].trim()), parseFloat(parts[1].trim())];
    });

    let fillColor = "#ef4444";
    if (mode === "above") fillColor = "#f59e0b";
    if (mode === "below") fillColor = "#3b82f6";

    const polygon = L.polygon(coordPairs, { color: fillColor, fillColor: fillColor, fillOpacity: 0.35 }).addTo(map);
    polygon.bindPopup(`<b>${name}</b><br>Mode: ${mode}<br>Alt Limit: ${altLimit}m`);

    geofences.push({
        name: name,
        coords: coordPairs,
        mode: mode,
        alt_limit: parseFloat(altLimit),
        end_time: endTime,
        never_expires: neverExpires,
        layer: polygon
    });
    
    localStorage.setItem('dms_geofences', JSON.stringify(getStorageGeofences()));

    closeModal('geofenceModal');
    alert(`Restricted Area "${name}" Added Locally!`);
}

function saveBlacklist() {
    const devId = document.getElementById('bl_device_id').value;
    const reason = document.getElementById('bl_reason').value;

    fetch('/api/blacklist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: devId, reason: reason })
    }).then(res => res.json()).then(res => {
        alert("Device added to Blacklist!");
        closeModal('blacklistModal');
    });
}

function saveUser() {
    const username = document.getElementById('new_username').value;
    const password = document.getElementById('new_password').value;
    const role = document.getElementById('new_role').value;

    if (!username || !password) {
        return alert("Please enter username and password!");
    }

    fetch('/api/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username, password: password, role: role })
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) {
            alert(`User '${username}' created!`);
            document.getElementById('new_username').value = '';
            document.getElementById('new_password').value = '';
            loadUsersList();
        } else {
            alert("Error: " + (data.error || "Failed to create user"));
        }
    });
}

function loadUsersList() {
    fetch('/api/users')
    .then(res => res.json())
    .then(users => {
        const container = document.getElementById('usersListContainer');
        container.innerHTML = '';
        users.forEach(u => {
            const div = document.createElement('div');
            div.style.display = 'flex';
            div.style.justifyContent = 'space-between';
            div.style.alignItems = 'center';
            div.style.marginBottom = '6px';
            div.style.fontSize = '0.85rem';
            
            div.innerHTML = `
                <span><strong>${u.username}</strong> (${u.role})</span>
                ${u.username !== 'admin' ? `<button onclick="deleteUser('${u.username}')" style="background:#ef4444; color:white; border:none; padding:2px 6px; border-radius:3px; cursor:pointer;">Delete</button>` : '<span style="color:#64748b;">Primary</span>'}
            `;
            container.appendChild(div);
        });
    });
}

function deleteUser(username) {
    if (!confirm(`Are you sure you want to delete user '${username}'?`)) return;

    fetch(`/api/users?username=${username}`, { method: 'DELETE' })
    .then(res => res.json())
    .then(data => {
        if (data.success) {
            alert(`User '${username}' deleted!`);
            loadUsersList();
        } else {
            alert("Error: " + (data.error || "Failed to delete user"));
        }
    });
}