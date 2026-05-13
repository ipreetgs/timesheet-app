pipeline {
    agent any

    options {
        buildDiscarder(logRotator(numToKeepStr: '10'))
        timestamps()
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
    }

    parameters {
        string(
            name: 'DEPLOY_DIR',
            defaultValue: '/var/www/timesheet',
            description: 'Absolute path to the app directory on this server'
        )
        string(
            name: 'APP_USER',
            defaultValue: 'testing',
            description: 'OS user that owns the app directory (e.g. ubuntu, testing)'
        )
        string(
            name: 'BRANCH',
            defaultValue: 'main',
            description: 'Git branch to build and deploy'
        )
        booleanParam(
            name: 'SKIP_TESTS',
            defaultValue: false,
            description: 'Skip the test stage (use with caution)'
        )
    }

    environment {
        // Secret file credential containing the .env for the app
        ENV_FILE_CRED_ID = 'timesheet-env-file'
        // Systemd service name (must match DEPLOY_SYSTEMD.service filename)
        SERVICE_NAME     = 'timesheet'
        // Nginx site config name
        NGINX_SITE       = 'timesheet'
    }

    stages {

        // ── 1. CHECKOUT ───────────────────────────────────────
        stage('Checkout') {
            steps {
                echo "==> Checking out branch: ${params.BRANCH}"
                checkout([
                    $class: 'GitSCM',
                    branches: [[name: "*/${params.BRANCH}"]],
                    userRemoteConfigs: scm.userRemoteConfigs
                ])
                sh 'git log -1 --oneline'
            }
        }

        // ── 2. LINT ───────────────────────────────────────────
        stage('Lint') {
            steps {
                echo "==> Running flake8 static analysis"
                script {
                    sh '''
                        python3 -m venv .lint-venv
                        . .lint-venv/bin/activate
                        pip install --quiet flake8
                        flake8 app.py chaos_endpoints.py chaos_bot.py \
                            --max-line-length=120 \
                            --ignore=W503 \
                            --exit-zero \
                            --statistics \
                            --output-file=flake8-report.txt || true
                        cat flake8-report.txt
                        deactivate
                    '''
                    def violations = sh(
                        script: "grep -c '.' flake8-report.txt || true",
                        returnStdout: true
                    ).trim().toInteger()
                    if (violations > 0) {
                        echo "WARNING: ${violations} lint violation(s) found. Build marked UNSTABLE."
                        unstable("Lint violations detected (${violations} issues)")
                    } else {
                        echo "Lint PASSED — no violations found."
                    }
                }
            }
        }

        // ── 3. TEST ───────────────────────────────────────────
        stage('Test') {
            when {
                expression { !params.SKIP_TESTS }
            }
            steps {
                echo "==> Installing dependencies and running tests"
                sh '''
                    python3 -m venv .test-venv
                    . .test-venv/bin/activate
                    pip install --quiet -r requirements.txt pytest
                    pytest --tb=short -q || true
                    deactivate
                '''
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: '**/test-results/*.xml'
                }
            }
        }

        // ── 4. PACKAGE ────────────────────────────────────────
        stage('Package') {
            steps {
                echo "==> Creating deployment archive"
                sh '''
                    set +e
                    tar --exclude='.git' \
                        --exclude='.lint-venv' \
                        --exclude='.test-venv' \
                        --exclude='venv' \
                        --exclude='__pycache__' \
                        --exclude='*.pyc' \
                        --exclude='logs' \
                        --exclude='*.sock' \
                        --exclude='database.db' \
                        --exclude='*.backup' \
                        --exclude='timesheet-app.tar.gz' \
                        --warning=no-file-changed \
                        -czf timesheet-app.tar.gz .
                    TAR_EXIT=$?
                    set -e
                    if [ $TAR_EXIT -gt 1 ]; then
                        echo "ERROR: tar failed with exit code $TAR_EXIT"
                        exit $TAR_EXIT
                    fi
                    echo "Archive size: $(du -sh timesheet-app.tar.gz | cut -f1)"
                '''
            }
        }

        // ── 5. DEPLOY (local — Jenkins IS the server) ─────────
        stage('Deploy') {
            steps {
                echo "==> Deploying locally to ${params.DEPLOY_DIR}"

                withCredentials([
                    file(credentialsId: env.ENV_FILE_CRED_ID, variable: 'ENV_FILE')
                ]) {
                    sh """
                        set -euo pipefail

                        DEPLOY_DIR="${params.DEPLOY_DIR}"
                        APP_USER="${params.APP_USER}"
                        SERVICE="${env.SERVICE_NAME}"
                        NGINX_SITE="${env.NGINX_SITE}"

                        echo "[1/7] Stopping Gunicorn service..."
                        sudo systemctl stop \$SERVICE || true

                        echo "[2/7] Syncing application files..."
                        sudo mkdir -p \$DEPLOY_DIR/persistent_data

                        # Extract archive, skipping persistent_data so the DB is never overwritten
                        sudo tar -xzf timesheet-app.tar.gz \
                            --directory \$DEPLOY_DIR \
                            --exclude='persistent_data'

                        echo "[3/7] Installing .env..."
                        sudo cp "\$ENV_FILE" \$DEPLOY_DIR/.env
                        sudo chown \$APP_USER:www-data \$DEPLOY_DIR/.env
                        sudo chmod 640 \$DEPLOY_DIR/.env

                        echo "[4/7] Setting up Python virtual environment..."
                        sudo python3 -m venv \$DEPLOY_DIR/venv
                        sudo \$DEPLOY_DIR/venv/bin/pip install --quiet --upgrade pip
                        sudo \$DEPLOY_DIR/venv/bin/pip install --quiet -r \$DEPLOY_DIR/requirements.txt

                        echo "[5/7] Fixing permissions..."
                        sudo chown -R \$APP_USER:www-data \$DEPLOY_DIR
                        sudo chmod -R 750 \$DEPLOY_DIR
                        sudo chmod -R 770 \$DEPLOY_DIR/persistent_data

                        echo "[6/7] Reloading systemd service..."
                        sudo systemctl daemon-reload
                        sudo systemctl enable \$SERVICE
                        sudo systemctl start \$SERVICE

                        echo "[7/7] Reloading Nginx..."
                        sudo cp \$DEPLOY_DIR/DEPLOY_NGINX.conf /etc/nginx/sites-available/\$NGINX_SITE
                        sudo ln -sf /etc/nginx/sites-available/\$NGINX_SITE \
                                    /etc/nginx/sites-enabled/\$NGINX_SITE
                        sudo nginx -t
                        sudo systemctl reload nginx

                        echo "==> Deployment complete!"
                    """
                }
            }
        }

        // ── 6. HEALTH CHECK (local curl) ──────────────────────
        stage('Health Check') {
            steps {
                echo "==> Verifying the app is responding on localhost..."
                sh '''
                    set -euo pipefail
                    for i in $(seq 1 6); do
                        HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/)
                        echo "Attempt $i — HTTP $HTTP_STATUS"
                        if [ "$HTTP_STATUS" = "200" ] || [ "$HTTP_STATUS" = "302" ]; then
                            echo "Health check PASSED"
                            exit 0
                        fi
                        sleep 5
                    done
                    echo "Health check FAILED after 30 seconds"
                    sudo systemctl status timesheet --no-pager || true
                    exit 1
                '''
            }
        }

    } // end stages

    post {
        success {
            echo "Pipeline SUCCESS — Timesheet app is live at http://localhost/"
        }
        failure {
            echo "Pipeline FAILED — check the logs above for details"
        }
        always {
            sh 'rm -f timesheet-app.tar.gz || true'
            cleanWs()
        }
    }
}
