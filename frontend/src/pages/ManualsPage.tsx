import {
  memo, useEffect, useLayoutEffect, useRef, useState,
  type ComponentPropsWithoutRef, type ReactNode, type Ref,
} from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Root, RootContent } from 'hast';
import { BookOpen, ChevronDown, List, Search } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import remarkGfm from 'remark-gfm';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';
import { apiFetch } from '../lib/api';
import '../styles/manual-reader.css';

interface ManualSummary {
  doc_id: string;
  title: string;
  snippet: string | null;
}

interface ManualDetail {
  doc_id: string;
  title: string;
  content: string;
}

interface Section {
  id: string;
  title: string;
  depth: number;
}

const MANUAL_PATHS: Record<string, string> = {
  'framework-readme': '/framework/README.md',
  'framework-setup': '/framework/SETUP.md',
  'dataset-format': '/framework/DATASET_FORMAT.md',
  'vlmevalkit-guide': '/framework/VLMEVALKIT.md',
  'datasets-readme': '/datasets/README.md',
};

function slugify(title: string) {
  return title.toLowerCase().replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, '').trim().replace(/\s+/g, '-') || 'section';
}

function headingText(node: RootContent): string {
  if (node.type === 'text') return node.value;
  if (node.type !== 'element') return '';
  if (node.tagName === 'img') return String(node.properties.alt ?? '');
  return node.children.map(headingText).join('');
}

function rehypeManualHeadings() {
  return (tree: Root) => {
    const ids = new Set<string>();

    function visit(node: Root | RootContent) {
      if (node.type === 'element' && /^h[1-6]$/.test(node.tagName) && !node.properties.id) {
        const base = slugify(headingText(node));
        let id = base;
        let suffix = 1;
        while (ids.has(id)) id = `${base}-${suffix++}`;
        ids.add(id);
        node.properties.id = id;
        node.properties.tabIndex = -1;
        node.properties.dataManualHeading = true;
      }
      if ('children' in node) node.children.forEach(visit);
    }

    visit(tree);
  };
}

function fragmentId(hash: string) {
  try {
    return decodeURIComponent(hash.slice(1));
  } catch {
    return hash.slice(1);
  }
}

function manualLocation(docId: string, hash = '') {
  return { pathname: '/toolkit/manuals', search: `?doc=${encodeURIComponent(docId)}`, hash };
}

function ManualLink({ href, docId, ...props }: ComponentPropsWithoutRef<'a'> & { docId: string }) {
  if (!href) return <span>{props.children}</span>;
  if (href.startsWith('#')) return <Link {...props} to={manualLocation(docId, href)} />;
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(href)) return <a {...props} href={href} />;

  const documentPath = MANUAL_PATHS[docId];
  if (documentPath) {
    const target = new URL(href, `https://manual.invalid${documentPath}`);
    const entry = Object.entries(MANUAL_PATHS).find(([, path]) => path === target.pathname);
    if (entry) return <Link {...props} to={manualLocation(entry[0], target.hash)} />;
  }

  return <span className="manual-file-reference" title={href}>{props.children} <code>{href}</code></span>;
}

const ManualArticle = memo(function ManualArticle({ doc, articleRef }: { doc: ManualDetail; articleRef: Ref<HTMLElement> }) {
  return (
    <article className="manual-prose" ref={articleRef} aria-label={doc.title} tabIndex={-1}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeManualHeadings]}
        skipHtml
        components={{
          a: ({ node: _node, ...props }) => <ManualLink {...props} docId={doc.doc_id} />,
          table: ({ node: _node, ...props }) => (
            <div className="manual-table-scroll" tabIndex={0} role="region" aria-label="手册表格">
              <table {...props} />
            </div>
          ),
        }}
      >
        {doc.content}
      </ReactMarkdown>
    </article>
  );
});

function ReaderSidebar({
  id, title, icon, children, expanded, toggle,
}: {
  id: string;
  title: string;
  icon: ReactNode;
  children: ReactNode;
  expanded: boolean;
  toggle: () => void;
}) {
  return (
    <aside className={`manual-sidebar manual-sidebar--${id}`}>
      <div className="manual-sidebar-title">{icon}{title}</div>
      <button
        type="button"
        className="manual-sidebar-toggle"
        aria-expanded={expanded}
        aria-controls={`manual-${id}`}
        onClick={toggle}
      >
        {icon}
        <span>{title}</span>
        <ChevronDown aria-hidden="true" size={15} />
      </button>
      <div id={`manual-${id}`} className="manual-sidebar-content" data-expanded={expanded}>
        {children}
      </div>
    </aside>
  );
}

export function ManualsPage() {
  const [params] = useSearchParams();
  const location = useLocation();
  const [search, setSearch] = useState('');
  const [keyword, setKeyword] = useState('');
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [outlineOpen, setOutlineOpen] = useState(false);
  const [outline, setOutline] = useState<{ docId: string; sections: Section[] }>();
  const articleRef = useRef<HTMLElement>(null);
  const previousDoc = useRef<string>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setKeyword(search.trim()), 180);
    return () => window.clearTimeout(timer);
  }, [search]);

  const catalogQ = useQuery({
    queryKey: ['toolkit-manuals', ''],
    queryFn: () => apiFetch<{ items: ManualSummary[] }>('/api/v1/toolkit/manuals'),
  });
  const searchQ = useQuery({
    queryKey: ['toolkit-manuals', keyword],
    enabled: Boolean(keyword),
    queryFn: () => apiFetch<{ items: ManualSummary[] }>(`/api/v1/toolkit/manuals?q=${encodeURIComponent(keyword)}`),
  });
  const results = keyword ? searchQ : catalogQ;
  const available = catalogQ.data?.items;
  const selectedId = params.get('doc') || available?.find((item) => item.doc_id === 'framework-readme')?.doc_id || available?.[0]?.doc_id;
  const docQ = useQuery({
    queryKey: ['toolkit-manual-detail', selectedId],
    enabled: Boolean(selectedId),
    queryFn: () => apiFetch<ManualDetail>(`/api/v1/toolkit/manuals/${encodeURIComponent(selectedId!)}`),
  });
  const doc = docQ.data;
  const activeSection = fragmentId(location.hash);

  useLayoutEffect(() => {
    if (!doc || !articleRef.current) return;
    const sections = Array.from(articleRef.current.querySelectorAll<HTMLElement>('[data-manual-heading]'), (heading) => ({
      id: heading.id,
      title: heading.textContent?.trim() || '章节',
      depth: Number(heading.tagName.slice(1)),
    }));
    setOutline({ docId: doc.doc_id, sections });
  }, [doc]);

  useLayoutEffect(() => {
    const article = articleRef.current;
    if (!doc || !article) return;
    if (activeSection) {
      const targets = Array.from(article.querySelectorAll<HTMLElement>('[id]'));
      const target = targets.find((element) => element.id === activeSection) ||
        targets.find((element) => element.id === slugify(activeSection));
      if (target) {
        target.scrollIntoView({ block: 'start', behavior: 'instant' });
        if (!target.hasAttribute('tabindex') && target.tabIndex < 0) target.tabIndex = -1;
        target.focus({ preventScroll: true });
      }
    } else if (previousDoc.current && previousDoc.current !== doc.doc_id) {
      window.scrollTo({ top: 0, behavior: 'instant' });
      article.focus({ preventScroll: true });
    }
    previousDoc.current = doc.doc_id;
  }, [doc, activeSection, location.key]);

  const sections = outline?.docId === doc?.doc_id ? outline?.sections : undefined;
  const firstDepth = sections?.length ? Math.min(...sections.map(({ depth }) => depth)) : 1;

  return (
    <div className="manual-reader">
      <header className="manual-reader-header">
        <BookOpen size={22} aria-hidden="true" />
        <h1>Manuals 框架与平台手册中心</h1>
      </header>

      <div className="manual-reader-grid">
        <ReaderSidebar
          id="catalog"
          title="手册检索"
          icon={<BookOpen aria-hidden="true" size={16} />}
          expanded={catalogOpen}
          toggle={() => setCatalogOpen(!catalogOpen)}
        >
          <label className="manual-search">
            <Search aria-hidden="true" size={16} />
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.currentTarget.value)}
              placeholder="搜索手册"
              aria-label="搜索手册"
            />
          </label>
          <nav aria-label="手册列表" className="manual-catalog">
            {results.isLoading ? (
              <TableSkeleton rows={4} cols={1} />
            ) : results.isError ? (
              <StateNoticeCard kind="error" title="手册列表加载失败" onRetry={() => results.refetch()} />
            ) : results.data?.items.length ? (
              <ul>
                {results.data.items.map((item) => {
                  const [title, filename] = /^(.*?)\s+\(([^)]+\.md)\)$/i.exec(item.title)?.slice(1) ?? [item.title];
                  return (
                    <li key={item.doc_id}>
                      <Link
                        to={manualLocation(item.doc_id)}
                        aria-current={item.doc_id === selectedId ? 'page' : undefined}
                        onClick={() => {
                          setCatalogOpen(false);
                          setOutlineOpen(false);
                        }}
                      >
                        <span>{title}</span>
                        {filename && <span className="manual-doc-filename">{filename}</span>}
                        {item.snippet && <small>{item.snippet}</small>}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            ) : <p className="manual-sidebar-empty">没有匹配的手册。</p>}
          </nav>
        </ReaderSidebar>

        <ReaderSidebar
          id="outline"
          title="On this page"
          icon={<List aria-hidden="true" size={16} />}
          expanded={outlineOpen}
          toggle={() => setOutlineOpen(!outlineOpen)}
        >
          <nav aria-label="章节目录" className="manual-outline">
            {docQ.isLoading ? <p className="manual-sidebar-empty">章节加载中…</p> : sections?.length && doc ? (
              <ul>
                {sections.map(({ id, title, depth }) => (
                  <li key={id}>
                    <Link
                      to={manualLocation(doc.doc_id, `#${encodeURIComponent(id)}`)}
                      aria-current={activeSection === id ? 'location' : undefined}
                      style={{ paddingInlineStart: `${(depth - firstDepth) * 0.62 + 0.55}rem` }}
                      onClick={() => setOutlineOpen(false)}
                    >
                      {title}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : <p className="manual-sidebar-empty">暂无章节。</p>}
          </nav>
        </ReaderSidebar>

        <section className="manual-reading-pane" aria-label="手册正文">
          {docQ.isLoading || (!selectedId && catalogQ.isLoading) ? (
            <TableSkeleton rows={9} cols={1} />
          ) : docQ.isError ? (
            <StateNoticeCard kind="error" title="手册正文加载失败" onRetry={() => docQ.refetch()} />
          ) : doc ? (
            <ManualArticle key={doc.doc_id} doc={doc} articleRef={articleRef} />
          ) : <StateNoticeCard title="暂无可阅读的手册" />}
        </section>
      </div>
    </div>
  );
}
