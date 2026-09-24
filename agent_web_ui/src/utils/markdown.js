import { marked } from 'marked';

marked.setOptions({
  breaks: true, // Enable line breaks
  gfm: true,    // Enable GitHub Flavored Markdown
});

export const renderMarkdown = (text) => {
  if (!text) return '';
  try {
    return marked.parse(text);
  } catch (e) {
    console.error('Markdown parsing error:', e);
    return text;
  }
};
