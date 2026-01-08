// @ts-check
import eslint from '@eslint/js';
import tseslint from 'typescript-eslint';
import eslintConfigPrettier from 'eslint-config-prettier/flat';

export default tseslint.config(
  // Ignore patterns
  {
    ignores: ['**/dist/**', '**/node_modules/**', '**/*.js'],
  },
  // Base configs
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  // Prettier must be last to disable conflicting rules
  eslintConfigPrettier,
  // Project-specific settings
  {
    files: ['packages/whatsapp-client/src/**/*.ts'],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  }
);
