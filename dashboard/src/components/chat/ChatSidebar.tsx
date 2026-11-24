import type React from "react";
import {
  ArchiveRestore,
  CheckSquare,
  FolderTree,
  GitBranch,
  MessageSquare,
  Pin,
  Plus,
  Search,
  Trash2
} from "lucide-react";
import type { ConversationSummary } from "../../types";
import { WorkspaceExplorer, type WorkspaceExplorerProps } from "./WorkspaceExplorer";
import { fmtShortDate } from "./chatHelpers";

export type SidebarMode = "chats" | "files" | "changes";

export interface ChatSidebarProps {
  sidebarCollapsed: boolean;
  sidebarMode: SidebarMode;
  setSidebarMode: (mode: SidebarMode) => void;
  dirtyCount: number;
  workspaceProps: Omit<WorkspaceExplorerProps, "mode">;
  streaming: boolean;
  searchOpen: boolean;
  setSearchOpen: React.Dispatch<React.SetStateAction<boolean>>;
  sidebarQuery: string;
  setSidebarQuery: (q: string) => void;
  searchInputRef: React.RefObject<HTMLInputElement | null>;
  selectMode: boolean;
  setSelectMode: (val: boolean) => void;
  exitSelectMode: () => void;
  sidebarView: "chats" | "trash";
  switchSidebarView: (view: "chats" | "trash") => void;
  conversations: ConversationSummary[];
  trash: ConversationSummary[];
  filteredConversations: ConversationSummary[];
  activeId: string | null;
  setActiveId: (id: string) => void;
  selectAllId: string;
  selectAllRef: React.RefObject<HTMLInputElement | null>;
  selectRowPrefix: string;
  supportsHasSelector: boolean;
  shiftHeldRef: React.MutableRefObject<boolean>;
  allVisibleSelected: boolean;
  visibleSelectedCount: number;
  selectedIds: Set<string>;
  toggleSelectAllVisible: () => void;
  handleSelectChange: (idx: number, id: string, checked: boolean) => void;
  createConversation: () => Promise<ConversationSummary | null>;
  patchConversation: (convId: string, patch: Partial<ConversationSummary>) => Promise<void>;
  requestTrashConversation: (conv: ConversationSummary) => void;
  restoreConversation: (conv: ConversationSummary) => Promise<void>;
  requestPermanentDelete: (conv: ConversationSummary) => void;
  requestBulkTrash: () => void;
  bulkRestoreSelected: () => Promise<void>;
  requestBulkPermanentDelete: () => void;
  requestEmptyTrash: () => void;
}

export function ChatSidebar({
  sidebarCollapsed,
  sidebarMode,
  setSidebarMode,
  dirtyCount,
  workspaceProps,
  streaming,
  searchOpen,
  setSearchOpen,
  sidebarQuery,
  setSidebarQuery,
  searchInputRef,
  selectMode,
  setSelectMode,
  exitSelectMode,
  sidebarView,
  switchSidebarView,
  conversations,
  trash,
  filteredConversations,
  activeId,
  setActiveId,
  selectAllId,
  selectAllRef,
  selectRowPrefix,
  supportsHasSelector,
  shiftHeldRef,
  allVisibleSelected,
  visibleSelectedCount,
  selectedIds,
  toggleSelectAllVisible,
  handleSelectChange,
  createConversation,
  patchConversation,
  requestTrashConversation,
  restoreConversation,
  requestPermanentDelete,
  requestBulkTrash,
  bulkRestoreSelected,
  requestBulkPermanentDelete,
  requestEmptyTrash
}: ChatSidebarProps) {
  return (
    <aside
      className={`chat-sidebar ${sidebarCollapsed ? "chat-sidebar--collapsed" : ""}`}
      aria-label="Workspace & Conversations"
    >
      <div className="sidebar-mode-tabs" role="tablist" aria-label="Sidebar mode">
        <button
          type="button"
          role="tab"
          aria-selected={sidebarMode === "chats"}
          className={`sidebar-mode-tab ${sidebarMode === "chats" ? "sidebar-mode-tab--active" : ""}`}
          onClick={() => setSidebarMode("chats")}
        >
          <MessageSquare size={12} aria-hidden="true" />
          <span>Chats</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={sidebarMode === "files"}
          className={`sidebar-mode-tab ${sidebarMode === "files" ? "sidebar-mode-tab--active" : ""}`}
          onClick={() => setSidebarMode("files")}
        >
          <FolderTree size={12} aria-hidden="true" />
          <span>Files</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={sidebarMode === "changes"}
          className={`sidebar-mode-tab ${sidebarMode === "changes" ? "sidebar-mode-tab--active" : ""}`}
          onClick={() => setSidebarMode("changes")}
        >
          <GitBranch size={12} aria-hidden="true" />
          <span>Changes</span>
          {dirtyCount > 0 && <span className="sidebar-mode-badge">{dirtyCount}</span>}
        </button>
      </div>

      {sidebarMode !== "chats" ? (
        <WorkspaceExplorer mode={sidebarMode} {...workspaceProps} />
      ) : (
        <>
          <div className="chat-sidebar__head">
        <button
          type="button"
          className="btn chat-new-btn"
          onClick={() => void createConversation()}
          disabled={streaming}
        >
          <Plus size={14} aria-hidden="true" />
          New Chat
        </button>
        <button
          type="button"
          className={`btn btn--sm chat-icon-btn ${searchOpen || sidebarQuery ? "btn--accent" : ""}`}
          aria-label="Filter conversations"
          aria-pressed={searchOpen || Boolean(sidebarQuery)}
          title="Search conversations"
          onClick={() => {
            setSearchOpen((v) => {
              const next = !v;
              if (next) window.setTimeout(() => searchInputRef.current?.focus(), 20);
              return next;
            });
          }}
        >
          <Search size={13} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={`btn btn--sm ${selectMode ? "btn--accent" : ""}`}
          aria-pressed={selectMode}
          title={selectMode ? "Exit multi-select (Esc)" : "Select multiple chats"}
          disabled={
            streaming ||
            (!selectMode && (sidebarView === "chats" ? conversations.length === 0 : trash.length === 0))
          }
          onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
        >
          <CheckSquare size={12} aria-hidden="true" />
          <span>{selectMode ? "Done" : "Select"}</span>
        </button>
      </div>

      <div
        className={`chat-sidebar__search ${
          searchOpen || sidebarQuery ? "chat-sidebar__search--open" : ""
        }`}
      >
        <Search size={12} aria-hidden="true" className="muted" />
        <input
          ref={searchInputRef}
          type="search"
          value={sidebarQuery}
          onChange={(e) => setSidebarQuery(e.target.value)}
          placeholder={sidebarView === "chats" ? "Filter chats…" : "Filter trash…"}
          aria-label="Filter conversations"
        />
      </div>

      <div className="chat-sidebar__tabs" role="group" aria-label="Conversation views">
        <button
          type="button"
          className="chat-sidebar__tab"
          aria-pressed={sidebarView === "chats"}
          onClick={() => switchSidebarView("chats")}
        >
          Chats ({conversations.length})
        </button>
        <button
          type="button"
          className="chat-sidebar__tab"
          aria-pressed={sidebarView === "trash"}
          onClick={() => switchSidebarView("trash")}
        >
          <Trash2 size={11} aria-hidden="true" />
          Trash ({trash.length})
        </button>
      </div>

      {selectMode && (
        <div className="chat-bulk-bar" role="group" aria-label="Bulk actions">
          <div className="chat-bulk-bar__top">
            <div className="chat-bulk-select-all">
              <input
                id={selectAllId}
                ref={selectAllRef}
                type="checkbox"
                className="chat-select-box"
                checked={allVisibleSelected}
                disabled={filteredConversations.length === 0}
                onChange={toggleSelectAllVisible}
              />
              <label htmlFor={selectAllId} className="chat-bulk-select-all__label">
                All ({visibleSelectedCount}/{filteredConversations.length})
              </label>
            </div>
            <span className="muted" style={{ fontSize: "0.68rem" }}>
              Shift+click range
            </span>
          </div>
          <div className="row-actions">
            {sidebarView === "chats" ? (
              <button
                type="button"
                className="btn btn--sm btn--danger"
                style={{ flex: 1, justifyContent: "center" }}
                disabled={streaming || selectedIds.size === 0}
                onClick={requestBulkTrash}
              >
                <Trash2 size={12} aria-hidden="true" />
                Delete ({selectedIds.size})
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className="btn btn--sm"
                  style={{ flex: 1, justifyContent: "center" }}
                  disabled={streaming || selectedIds.size === 0}
                  onClick={() => void bulkRestoreSelected()}
                >
                  <ArchiveRestore size={12} aria-hidden="true" />
                  Restore ({selectedIds.size})
                </button>
                <button
                  type="button"
                  className="btn btn--sm btn--danger"
                  style={{ flex: 1, justifyContent: "center" }}
                  disabled={streaming || selectedIds.size === 0}
                  onClick={requestBulkPermanentDelete}
                >
                  <Trash2 size={12} aria-hidden="true" />
                  Erase ({selectedIds.size})
                </button>
              </>
            )}
          </div>
        </div>
      )}

      {sidebarView === "trash" && !selectMode && trash.length > 0 && (
        <button
          type="button"
          className="btn btn--sm btn--danger"
          disabled={streaming}
          onClick={requestEmptyTrash}
        >
          <Trash2 size={12} aria-hidden="true" />
          Empty Trash ({trash.length})
        </button>
      )}

      {selectMode ? (
        <fieldset className="chat-conv-list chat-conv-fieldset">
          <legend className="visually-hidden">
            Select {sidebarView === "chats" ? "conversations" : "trashed conversations"}
          </legend>
          {filteredConversations.length === 0 ? (
            <p className="muted chat-conv-empty">
              {sidebarView === "chats" ? "No matching chats." : "Trash bin is empty."}
            </p>
          ) : (
            filteredConversations.map((c, idx) => {
              const cbId = `${selectRowPrefix}-${c.id}`;
              const checked = selectedIds.has(c.id);
              return (
                <div
                  key={c.id}
                  className={`chat-conv-item chat-conv-item--selectable ${
                    sidebarView === "trash" ? "chat-conv-item--trash" : ""
                  } ${!supportsHasSelector && checked ? "chat-conv-item--checked" : ""}`}
                  onMouseDown={(e) => {
                    shiftHeldRef.current = e.shiftKey;
                  }}
                >
                  <input
                    id={cbId}
                    type="checkbox"
                    className="chat-select-box"
                    checked={checked}
                    disabled={streaming}
                    onKeyDown={(e) => {
                      shiftHeldRef.current = e.shiftKey;
                    }}
                    onChange={(e) => handleSelectChange(idx, c.id, e.target.checked)}
                  />
                  <label htmlFor={cbId} className="chat-conv-item__label">
                    <span className="chat-conv-item__title">
                      {c.pinned && sidebarView === "chats" && (
                        <Pin size={11} aria-hidden="true" className="chat-pin-badge" />
                      )}
                      {c.title}
                    </span>
                    <span className="chat-conv-item__meta">
                      <code>{c.model}</code>
                      <span>
                        {sidebarView === "trash"
                          ? c.deleted_at
                            ? fmtShortDate(c.deleted_at)
                            : ""
                          : fmtShortDate(c.updated_at)}
                      </span>
                    </span>
                  </label>
                </div>
              );
            })
          )}
        </fieldset>
      ) : (
        <ul className="chat-conv-list">
          {filteredConversations.length === 0 ? (
            <li className="muted chat-conv-empty">
              {sidebarView === "chats" ? "No conversations." : "Trash is empty."}
            </li>
          ) : sidebarView === "chats" ? (
            filteredConversations.map((c) => {
              const isSelected = c.id === activeId;
              return (
                <li
                  key={c.id}
                  className={`chat-conv-item ${isSelected ? "chat-conv-item--active" : ""} ${
                    c.pinned ? "chat-conv-item--pinned" : ""
                  }`}
                >
                  <button
                    type="button"
                    className="chat-conv-item__btn"
                    onClick={() => !streaming && setActiveId(c.id)}
                  >
                    <span className="chat-conv-item__title">
                      <span
                        className={`chat-conv-dot ${isSelected ? "chat-conv-dot--active" : ""}`}
                        aria-hidden="true"
                      />
                      <span className="chat-conv-item__text">{c.title}</span>
                    </span>
                  </button>
                  <div className="row-actions chat-conv-item__actions">
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost chat-pin-btn"
                      aria-label={`Pin ${c.title}`}
                      aria-pressed={c.pinned}
                      title={c.pinned ? "Unpin chat" : "Pin chat to top"}
                      disabled={streaming}
                      onClick={() => void patchConversation(c.id, { pinned: !c.pinned })}
                    >
                      <Pin size={12} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost btn--danger"
                      aria-label={`Move ${c.title} to trash`}
                      disabled={streaming}
                      onClick={() => requestTrashConversation(c)}
                    >
                      <Trash2 size={12} aria-hidden="true" />
                    </button>
                  </div>
                </li>
              );
            })
          ) : (
            filteredConversations.map((c) => (
              <li key={c.id} className="chat-conv-item chat-conv-item--trash">
                <div className="chat-conv-item__btn">
                  <span className="chat-conv-item__title">{c.title}</span>
                  <span className="chat-conv-item__meta">
                    <code>{c.model}</code>
                    <span>{c.deleted_at ? fmtShortDate(c.deleted_at) : ""}</span>
                  </span>
                </div>
                <div className="row-actions">
                  <button
                    type="button"
                    className="btn btn--sm btn--ghost"
                    aria-label={`Restore ${c.title}`}
                    title="Restore chat"
                    disabled={streaming}
                    onClick={() => void restoreConversation(c)}
                  >
                    <ArchiveRestore size={12} aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm btn--ghost btn--danger"
                    aria-label={`Permanently delete ${c.title}`}
                    title="Delete forever"
                    disabled={streaming}
                    onClick={() => requestPermanentDelete(c)}
                  >
                    <Trash2 size={12} aria-hidden="true" />
                  </button>
                </div>
              </li>
            ))
          )}
        </ul>
      )}
        </>
      )}
    </aside>
  );
}
