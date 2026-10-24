import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'
import prettier from 'eslint-config-prettier'

const workflowFiles = [
  'src/utils/graph/**/*.ts',
  'src/utils/graph/**/*.vue',
  'src/composables/workflow/**/*.ts',
  'src/composables/workflow/**/*.vue',
  'src/stores/workflow/**/*.ts',
  'src/stores/workflow/**/*.vue',
  'src/api/workflow/**/*.ts',
  'src/api/workflow/**/*.vue',
  'src/components/workflow/**/*.ts',
  'src/components/workflow/**/*.vue',
  'src/views/WorkflowView.vue',
]

const layerRule = (group, message) => [
  'error',
  {
    patterns: [{ group, message }],
  },
]

export default [
  {
    ignores: [
      'dist/**',
      'coverage/**',
      'node_modules/**',
      'test-results/**',
      'src/components/workflow/__tests__/**',
    ],
  },
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        extraFileExtensions: ['.vue'],
        sourceType: 'module',
      },
    },
  },
  {
    files: workflowFiles,
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-non-null-assertion': 'error',
      'vue/no-v-html': 'error',
    },
  },
  {
    files: ['src/utils/graph/**/*.ts', 'src/utils/graph/**/*.vue'],
    rules: {
      'no-restricted-imports': layerRule(
        [
          'vue',
          'pinia',
          'axios',
          '@vue-flow/*',
          '@vue-flow/**',
          'ant-design-vue',
          '@/api/**',
          '@/stores/**',
          '@/components/**',
          '@/composables/**',
        ],
        'domain 层（utils/graph）不得依赖 Vue/Pinia/axios/Vue Flow/UI 或上层目录',
      ),
    },
  },
  {
    files: ['src/composables/workflow/**/*.ts', 'src/composables/workflow/**/*.vue'],
    rules: {
      'no-restricted-imports': layerRule(
        ['axios', '@/stores/**', '@/components/**', '@/api/workflow/client'],
        'composables 层不得依赖 axios/store/组件或具体 gateway 实现',
      ),
    },
  },
  {
    files: ['src/stores/workflow/**/*.ts', 'src/stores/workflow/**/*.vue'],
    rules: {
      'no-restricted-imports': layerRule(
        ['axios', '@/components/**', '@/api/workflow/client'],
        'store 层不得依赖 axios/组件或具体 gateway 实现',
      ),
    },
  },
  {
    files: ['src/components/workflow/**/*.ts', 'src/components/workflow/**/*.vue'],
    rules: {
      'no-restricted-imports': layerRule(
        ['axios', '@/api/workflow/client'],
        '组件层不得直接使用 axios 或具体 gateway 实现',
      ),
    },
  },
  {
    files: ['src/api/workflow/client.ts'],
    rules: {
      'no-restricted-imports': layerRule(
        ['vue', '@/stores/**', '@/components/**'],
        'gateway 实现不得依赖视图与状态层',
      ),
    },
  },
  {
    files: ['src/views/WorkflowView.vue'],
    rules: {
      'no-restricted-imports': layerRule(['@/api/workflow/client'], '视图只能通过 store/gateway 契约访问 API'),
    },
  },
  prettier,
]
