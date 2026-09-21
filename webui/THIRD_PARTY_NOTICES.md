# Web UI third-party assets

## Brand wordmark

`public/mclaw-wordmark-fold.svg` is project-specific vector artwork and does not use an external font. `public/mclaw-logo.png` is the circular brand mark supplied by the project.

## Welcome artwork

The five M, C, L, A and W letter sculptures, doorway and door leaf in `public/welcome-letters/` were created for M-Claw with Image Gen. Layouts and animation in `WelcomeCompanion` are project code. These images ship locally with the application.

## Provider logos

The selected SVGs in `public/provider-logos/` are copied from [`@lobehub/icons-static-svg`](https://www.npmjs.com/package/@lobehub/icons-static-svg), version `1.95.0`, published by LobeHub. The MIT license is preserved in `public/licenses/lobe-icons-LICENSE`. Logos identify the corresponding service providers; associated names and trademarks belong to their owners. Unsupported or unavailable logos use a generic connection icon with the provider name always visible.

The Xiaomi MiMo, xAI, Amazon Bedrock, and SiliconFlow mappings use the package's `xiaomimimo.svg`, `xai.svg`, `bedrock.svg`, and `siliconcloud-color.svg` assets respectively. OpenRouter, Yi, and Bedrock use the monochrome variants for contrast on white backgrounds. The original white Kimi mark is displayed on a dark background. SVG artwork is copied unchanged; display size and backgrounds are provided by the UI stylesheet.

## Frontend dependencies

React, React DOM, Vite, TypeScript, Lucide, react-markdown, remark-gfm, and test tooling are installed through the checked-in npm lockfile and retain their package licenses.

## Channel logos

WeChat (filled) and DingTalk (outlined) SVG marks are from [Ant Design Icons](https://github.com/ant-design/ant-design-icons/tree/7f2516ac91226d2b41f93b35cb5197c8d94f7189/packages/icons-svg/svg), revision `7f2516ac91226d2b41f93b35cb5197c8d94f7189`, under the MIT license. Brand colors were added. The license is included at `public/licenses/ant-design-icons-LICENSE`. These marks identify their respective channels.
