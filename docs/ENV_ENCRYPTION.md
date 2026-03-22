# 环境变量加密方案（GPG）

本项目使用 GPG 加密来安全地管理环境变量配置。加密后的 `.env.gpg` 文件可以提交到 Git 仓库，而解密密码只需在 GitHub 配置一次。

## 快速开始

### 1. 安装 GPG

**macOS:**
```bash
brew install gnupg
```

**Ubuntu/Debian:**
```bash
sudo apt-get install gnupg
```

**Windows:**
下载并安装 [Gpg4win](https://www.gpg4win.org/)

### 2. 加密 .env 文件

```bash
# 设置加密密码（替换为你的密码）
```bash
# 设置加密密码（替换为你的密码）
export GPG_PASSPHRASE="666"

# 加密 .env 文件
gpg --symmetric --cipher-algo AES256 --compress-algo 1 --s2k-digest-algo SHA512 \
    --passphrase "$GPG_PASSPHRASE" --batch --yes --output .env.gpg .env

# 验证加密成功
ls -la .env.gpg
```

# 加密 .env 文件
gpg --symmetric --cipher-algo AES256 --compress-algo 1 --s2k-digest-algo SHA512 \
    --passphrase "$GPG_PASSPHRASE" --batch --yes --output .env.gpg .env

# 验证加密成功
ls -la .env.gpg
```

### 3. 提交加密文件

```bash
# .env 文件已在 .gitignore 中，不会被提交
git add .env.gpg
git commit -m "Add encrypted environment configuration"
git push
```

### 4. 在 GitHub 配置解密密码

1. 打开 GitHub 仓库页面
2. 进入 **Settings** → **Secrets and variables** → **Actions**
3. 点击 **New repository secret**
4. 名称填写：`ENV_GPG_PASSPHRASE`
5. 值填写：你的加密密码
6. 点击 **Add secret**

## 解密文件

### 本地解密

```bash
# 使用密码解密
gpg --decrypt --passphrase "your-secret-password" --batch --yes \
    --output .env .env.gpg
```

### 自动解密（GitHub Actions）

GitHub Actions 工作流已配置自动解密步骤，会在执行分析前自动解密 `.env.gpg` 文件。

## 更新配置

当需要修改环境变量时：

```bash
# 1. 解密现有配置
gpg --decrypt --passphrase "your-secret-password" --batch --yes \
    --output .env .env.gpg

# 2. 修改 .env 文件
# ... 编辑 .env ...

# 3. 重新加密
gpg --symmetric --cipher-algo AES256 --compress-algo 1 --s2k-digest-algo SHA512 \
    --passphrase "$GPG_PASSPHRASE" --batch --yes --output .env.gpg .env

# 4. 提交更新
git add .env.gpg
git commit -m "Update encrypted environment configuration"
git push
```

## 安全建议

1. **密码强度**：使用至少 16 位的强密码，包含大小写字母、数字和特殊字符
2. **密码管理**：将密码保存在密码管理器中，不要明文存储
3. **定期更换**：建议定期更换加密密码
4. **访问控制**：限制知道密码的人员范围

## 故障排除

### 解密失败

```bash
# 检查文件是否存在
ls -la .env.gpg

# 验证密码是否正确
gpg --decrypt --passphrase "your-password" --batch .env.gpg
```

### 权限问题

```bash
# 确保文件权限正确
chmod 600 .env.gpg
chmod 600 .env
```

## 替代方案

如果不想使用 GPG 加密，可以继续使用传统的 GitHub Secrets 方式配置环境变量。但 GPG 加密方案的优势在于：

- ✅ 只需配置一个密码
- ✅ 环境变量集中管理，易于查看和修改
- ✅ 支持复杂的配置结构
- ✅ 本地和 CI 环境使用相同的配置方式
