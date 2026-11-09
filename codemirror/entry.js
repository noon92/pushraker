// pushraker's template editor: CodeMirror 6 with Jinja highlighting, line
// numbers and autocomplete. The page builds the editor with these.
import { EditorView, lineNumbers, keymap, drawSelection,
         highlightActiveLine, placeholder } from "@codemirror/view";
import { EditorState } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { StreamLanguage, syntaxHighlighting, defaultHighlightStyle,
         bracketMatching } from "@codemirror/language";
import { jinja2 } from "@codemirror/legacy-modes/mode/jinja2";
import { autocompletion, completionKeymap } from "@codemirror/autocomplete";
import { oneDark } from "@codemirror/theme-one-dark";

window.PushrakerCM = {
  EditorView, EditorState, lineNumbers, keymap, drawSelection,
  highlightActiveLine, placeholder, defaultKeymap, history, historyKeymap,
  StreamLanguage, syntaxHighlighting, defaultHighlightStyle, bracketMatching,
  jinja2, autocompletion, completionKeymap, oneDark,
};
