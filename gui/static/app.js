document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const chatDisplay = document.getElementById('chatDisplay');
    const chatForm = document.getElementById('chatForm');
    const msgInput = document.getElementById('msgInput');
    const sendBtn = document.getElementById('sendBtn');

    const pendingTitle = document.getElementById('pendingTitle');
    const changesList = document.getElementById('changesList');
    const emptyState = document.getElementById('emptyState');

    const commitBtn = document.getElementById('commitBtn');
    const undoBtn = document.getElementById('undoBtn');
    const resetBtn = document.getElementById('resetBtn');

    // --- UI Helpers ---
    const scrollToBottom = () => {
        chatDisplay.scrollTo({
            top: chatDisplay.scrollHeight,
            behavior: 'smooth'
        });
    };

    const formatTime = () => {
        const now = new Date();
        return now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    };

    const appendMessage = (sender, text) => {
        const msgWrapper = document.createElement('div');
        msgWrapper.className = `flex flex-col w-full animate-in fade-in duration-500`;

        if (sender === 'user') {
            msgWrapper.classList.add('items-end', 'slide-in-from-right-4');
            msgWrapper.innerHTML = `
                <div class="max-w-[85%] glass-card no-borders rounded-2xl rounded-tr-none px-5 py-4 inner-glow border-primary/20">
                    <p class="text-body-md text-on-surface whitespace-pre-wrap">${text}</p>
                </div>
                <span class="mt-2 text-label-sm font-label-sm text-outline px-1">${formatTime()}</span>
            `;
        } else if (sender === 'agent') {
            msgWrapper.classList.add('items-start', 'slide-in-from-left-4');
            // Parse Markdown for Agent's text
            const parsedText = marked.parse(text);
            msgWrapper.innerHTML = `
                <div class="flex items-center gap-3 mb-2">
                    <div class="w-8 h-8 rounded-lg bg-tertiary-container flex items-center justify-center border border-tertiary/20 shadow-lg shadow-tertiary/10">
                        <span class="material-symbols-outlined text-on-tertiary-container text-[18px]" data-icon="robot_2">robot_2</span>
                    </div>
                    <span class="text-label-md font-label-md font-bold text-tertiary">Agent Engine</span>
                </div>
                <div class="max-w-full space-y-4 w-full">
                    <div class="glass-card no-borders rounded-2xl rounded-tl-none px-5 py-4 inner-glow">
                        <div class="text-body-md text-on-surface markdown-body">${parsedText}</div>
                    </div>
                </div>
            `;
        } else if (sender === 'error') {
            msgWrapper.className = `flex justify-center mt-2 w-full animate-in fade-in duration-500`;
            msgWrapper.innerHTML = `
                <p class="text-sm font-medium text-error">${text}</p>
            `;
        } else { // system
            msgWrapper.className = `flex justify-center mt-2 w-full animate-in fade-in duration-500`;
            msgWrapper.innerHTML = `
                <p class="text-sm font-medium text-tertiary">${text}</p>
            `;
        }

        chatDisplay.appendChild(msgWrapper);
        scrollToBottom();
        return msgWrapper;
    };

    const showThinking = () => {
        const thinkingWrapper = document.createElement('div');
        thinkingWrapper.id = 'thinkingIndicator';
        thinkingWrapper.className = `flex flex-col items-start w-full animate-in fade-in slide-in-from-left-4 duration-500`;
        thinkingWrapper.innerHTML = `
            <div class="flex items-center gap-3 mb-2">
                <div class="w-8 h-8 rounded-lg bg-tertiary-container flex items-center justify-center border border-tertiary/20 shadow-lg shadow-tertiary/10">
                    <span class="material-symbols-outlined text-on-tertiary-container text-[18px]" data-icon="robot_2">robot_2</span>
                </div>
                <span class="text-label-md font-label-md font-bold text-tertiary">Agent Engine</span>
            </div>
            <div class="max-w-full">
                <div class="glass-card rounded-2xl rounded-tl-none px-5 py-4 inner-glow flex items-center gap-2">
                    <span class="material-symbols-outlined text-tertiary animate-spin" data-icon="progress_activity">progress_activity</span>
                    <p class="text-body-md text-on-surface italic text-tertiary">Thinking...</p>
                </div>
            </div>
        `;
        chatDisplay.appendChild(thinkingWrapper);
        scrollToBottom();
    };

    const hideThinking = () => {
        const thinkingDiv = document.getElementById('thinkingIndicator');
        if (thinkingDiv) {
            thinkingDiv.remove();
        }
    };

    const renderTable = (tableData, parentWrapper) => {
        if (!tableData || !tableData.columns || tableData.columns.length === 0) return;

        let contentDiv;
        if (parentWrapper) {
            contentDiv = parentWrapper.querySelector('.max-w-full.space-y-4');
        }
        if (!contentDiv) {
            contentDiv = chatDisplay;
        }

        const tableHtml = `
        <div class="glass-card rounded-xl overflow-hidden border border-white/10 shadow-2xl mt-4 animate-in fade-in slide-in-from-bottom-2 duration-500 w-full max-w-[100%] overflow-x-auto">
            <div class="px-4 py-2 bg-surface-container-highest/30 border-b border-white/5 flex justify-between min-w-max">
                <span class="text-label-sm font-label-sm text-outline">📊 Query Result</span>
                <span class="text-label-sm font-label-sm text-outline">${tableData.row_count} row${tableData.row_count !== 1 ? 's' : ''}</span>
            </div>
            <div class="overflow-x-auto max-h-[400px]">
                <table class="w-full text-left border-collapse min-w-max">
                    <thead>
                        <tr class="bg-surface-container-highest/50 border-b border-white/10">
                            ${tableData.columns.map(col => `<th class="px-4 py-3 text-label-md font-label-md text-primary font-bold sticky top-0 backdrop-blur-md z-10">${col}</th>`).join('')}
                        </tr>
                    </thead>
                    <tbody class="text-label-md font-label-md divide-y divide-white/5">
                        ${tableData.rows.map((row, idx) => `
                            <tr class="${idx % 2 === 0 ? 'bg-white/5' : ''} hover:bg-white/10 transition-colors">
                                ${row.map(val => `<td class="px-4 py-3 font-label-sm text-on-surface-variant">${val !== null ? val : 'NULL'}</td>`).join('')}
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
            ${tableData.row_count > 15 ? `<div class="px-4 py-2 bg-surface-container-highest/30 border-t border-white/5 text-center text-label-sm font-label-sm text-outline min-w-max">... (${tableData.row_count - 15} more rows not shown)</div>` : ''}
        </div>
        `;

        const tableContainer = document.createElement('div');
        tableContainer.innerHTML = tableHtml;
        contentDiv.appendChild(tableContainer.firstElementChild);
        scrollToBottom();
    };

    const updateSidebar = async () => {
        try {
            const res = await fetch('/api/pending');
            const data = await res.json();

            const changes = data.changes || [];
            const count = data.pending_count || 0;

            pendingTitle.textContent = `Pending Changes (${count})`;
            changesList.innerHTML = '';

            if (changes.length > 0) {
                emptyState.style.display = 'none';
                changes.forEach(change => {
                    const li = document.createElement('li');
                    li.className = 'p-3 rounded-xl bg-surface-container hover:bg-surface-container-high border border-white/5 transition-colors flex items-start gap-2';
                    li.innerHTML = `
                        <div class="mt-0.5 w-5 h-5 rounded-full bg-tertiary-container/30 flex items-center justify-center flex-shrink-0">
                            <span class="text-[10px] font-bold text-tertiary">${change.id}</span>
                        </div>
                        <p class="text-sm text-on-surface">${change.description || 'Unknown operation'}</p>
                    `;
                    changesList.appendChild(li);
                });
            } else {
                emptyState.style.display = 'block';
            }
        } catch (err) {
            console.error('Failed to update sidebar:', err);
        }
    };

    const setBusy = (busy) => {
        sendBtn.disabled = busy;
        msgInput.disabled = busy;
        if (!busy) msgInput.focus();
    };

    // --- Event Listeners ---
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const text = msgInput.value.trim();
        if (!text) return;

        appendMessage('user', text);
        msgInput.value = '';
        setBusy(true);
        showThinking();

        try {
            const res = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text })
            });

            const data = await res.json();
            hideThinking();

            if (data.error) {
                appendMessage('error', `⚠ Error: ${data.error}`);
            } else {
                const agentWrapper = appendMessage('agent', data.reply);
                if (data.table) {
                    renderTable(data.table, agentWrapper);
                }
                if (data.artifacts && window.ArtifactRenderers) {
                    window.ArtifactRenderers.renderAll(data.artifacts, agentWrapper);
                }
                updateSidebar(); // Always update in case of implicit mutations
            }
        } catch (err) {
            hideThinking();
            appendMessage('error', `⚠ Network Error: ${err.message}`);
        } finally {
            setBusy(false);
        }
    });

    commitBtn.addEventListener('click', async () => {
        try {
            const res = await fetch('/api/commit', { method: 'POST' });
            const data = await res.json();

            if (data.error === 'connection_lost') {
                const lost = data.lost_count || (data.lost || []).length;
                appendMessage('error', `⚠ Connection to the database dropped while your ${lost} pending change${lost !== 1 ? 's were' : ' was'} uncommitted. The server discarded them. Please re-stage and commit promptly.`);
            } else if (data.success) {
                const count = data.committed_count || 0;
                appendMessage('system', `✅ Committed ${count} change${count !== 1 ? 's' : ''} to disk.`);
            } else {
                appendMessage('system', `ℹ No pending changes to commit or commit failed.`);
            }
            updateSidebar();
        } catch (err) {
            appendMessage('error', `⚠ Commit Error: ${err.message}`);
        }
    });

    undoBtn.addEventListener('click', async () => {
        try {
            const res = await fetch('/api/undo', { method: 'POST' });
            const data = await res.json();

            if (data.error === 'connection_lost') {
                const lost = data.lost_count || (data.lost || []).length;
                appendMessage('error', `⚠ Connection to the database dropped while your ${lost} pending change${lost !== 1 ? 's were' : ' was'} uncommitted. The server discarded them.`);
            } else if (data.success && data.undone && data.undone.length > 0) {
                const desc = data.undone[0].description || 'last change';
                appendMessage('system', `↩ Undone: ${desc}`);
            } else {
                appendMessage('system', `ℹ Nothing to undo.`);
            }
            updateSidebar();
        } catch (err) {
            appendMessage('error', `⚠ Undo Error: ${err.message}`);
        }
    });

    resetBtn.addEventListener('click', async () => {
        if (!confirm("Clear the entire conversation history?\n\nThis does not affect pending database changes.")) {
            return;
        }

        try {
            await fetch('/api/reset', { method: 'POST' });
            // Clear chat display
            chatDisplay.innerHTML = `
                <div class="flex justify-center w-full mt-2">
                    <p class="text-sm font-medium text-tertiary">
                        Connected &middot; Type a message to start.
                    </p>
                </div>
            `;
            updateSidebar();
        } catch (err) {
            alert(`Reset Error: ${err.message}`);
        }
    });

    // Initial load
    updateSidebar();
});
